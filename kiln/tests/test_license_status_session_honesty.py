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
    "refreshed. Run `kiln signin` to sign in again."
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
    """Never leave the user with a false report and no next step.

    The next step is split by audience: ``action_required`` is what the person
    reads and says what happened in plain words, while the command they cannot
    type from a chat window rides in the agent-addressed fields beside it.
    """
    _install_session(monkeypatch, state="signed_out", detail="")
    payload = {"source": "oauth", "tier": "pro", "is_valid": True}
    _annotate_session_liveness(payload)
    assert payload["is_valid"] is False
    assert "signing in" in payload["action_required"].lower()
    assert "`" not in payload["action_required"], (
        "no command syntax in user-facing copy"
    )
    assert "kiln signin" in payload["agent_hint"]
    assert payload["setup_hint"] == "kiln signin"


# ---------------------------------------------------------------------------
# The `kiln serve` start-up banner: the same lie, one line earlier.
#
# 2026-09-27: a session the server had refused to renew (``needs_signin``
# on disk, every hosted call in the process answering "signed out") still
# launched as "✓ Signed in as adam@kiln3d.com (Free)" — the banner read the
# email straight off the token file and the tier off the free-tier stub,
# consulting neither the resolver's verdict nor the file's own ``tier``.
#
# These run against real token files and the real ``kiln.auth_session``
# (its file-only reading never touches the network), so they fail if the
# banner and the resolver ever disagree about what a file means.
# ---------------------------------------------------------------------------


def _write_token_file(tmp_path, monkeypatch, **fields):
    import json

    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    data = {"access_token": "x" * 40, "refresh_token": "r", "email": "adam@kiln3d.com",
            "tier": "enterprise", "has_entitlement": True}
    data.update(fields)
    (tmp_path / ".kiln").mkdir()
    (tmp_path / ".kiln" / "auth_tokens.json").write_text(json.dumps(data), encoding="utf-8")


def _banner(monkeypatch, capsys) -> str:
    import kiln.server as srv

    # Without kiln-pro the tier stub answers "free" regardless; pin that so
    # the test means the same thing on an install that has kiln-pro.
    monkeypatch.setattr(srv, "get_tier", lambda *a, **k: "free")
    srv._print_startup_banner()
    return capsys.readouterr().err.strip()


def test_banner_says_expired_not_signed_in_when_the_session_needs_signin(
    tmp_path, monkeypatch, capsys
):
    """The incident, pinned: the verdict on disk wins over the email beside
    it.  The file is exactly what the resolver leaves after a rejected
    refresh — email and tier kept, refresh token gone, the stamp set."""
    _write_token_file(
        tmp_path, monkeypatch, refresh_token="", refresh_rejected_at="2026-09-27T20:40:00Z",
    )

    line = _banner(monkeypatch, capsys)

    assert "Signed in as" not in line
    assert "adam@kiln3d.com" in line
    assert "(Enterprise)" in line
    assert "expired" in line
    assert "kiln signin" in line


def test_banner_prints_the_tier_the_token_file_records(tmp_path, monkeypatch, capsys):
    """"(Free)" beside an enterprise sign-in was the stub's answer, not the
    account's — the file carries the tier the server named at sign-in."""
    _write_token_file(tmp_path, monkeypatch)

    line = _banner(monkeypatch, capsys)

    assert line == "✓ Kiln MCP. Signed in as adam@kiln3d.com (Enterprise)."


def test_banner_never_pays_a_refresh_to_say_where_things_stand(
    tmp_path, monkeypatch, capsys
):
    """Start-up is not the moment: an expired-on-the-clock token is still a
    stored session, and the first tool call renews it as it always did."""
    import kiln.auth_session as auth_session

    def _explode(_rt):  # pragma: no cover — reaching here IS the failure
        raise AssertionError("the banner touched the network")

    monkeypatch.setattr(auth_session, "_post_refresh", _explode)
    _write_token_file(tmp_path, monkeypatch, access_token="not-a-jwt")

    line = _banner(monkeypatch, capsys)

    assert line == "✓ Kiln MCP. Signed in as adam@kiln3d.com (Enterprise)."


def test_banner_treats_an_email_with_no_access_token_as_signed_out(
    tmp_path, monkeypatch, capsys
):
    _write_token_file(tmp_path, monkeypatch, access_token="")

    line = _banner(monkeypatch, capsys)

    assert "Not signed in" in line
    assert "Signed in as" not in line


def test_banner_without_a_token_file_says_not_signed_in(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))

    line = _banner(monkeypatch, capsys)

    assert "Not signed in" in line


def test_banner_survives_a_broken_resolver(tmp_path, monkeypatch, capsys):
    """A broken banner beats a broken server: the file is all there is."""
    import kiln.auth_session as auth_session

    def _explode():
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(auth_session, "stored_session_state", _explode)
    _write_token_file(tmp_path, monkeypatch)

    line = _banner(monkeypatch, capsys)

    assert line == "✓ Kiln MCP. Signed in as adam@kiln3d.com (Enterprise)."
