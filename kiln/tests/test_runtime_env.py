"""The hosted-vs-local predicate — one answer, several callers.

Small module, but it decides two unrelated things: whether the heartbeat
reports this process as an install, and whether a user is told to go install
software.  Getting it wrong in the "hosted" direction means telling someone
to pip install onto a server they don't control; getting it wrong the other
way means the shared box reports itself as somebody's laptop.
"""

from __future__ import annotations

import pytest

from kiln.runtime_env import HOSTED_ENV_VAR, is_hosted_multitenant


def test_absent_means_local(monkeypatch):
    """Unset is the safe reading: somebody's own install.

    A local install that wrongly believed it was hosted would hide its own
    fix-it instructions, which is the worst direction to be wrong in.
    """
    monkeypatch.delenv(HOSTED_ENV_VAR, raising=False)
    assert is_hosted_multitenant() is False


@pytest.mark.parametrize("value", ["1", "true", "yes"])
def test_documented_truthy_values(monkeypatch, value):
    monkeypatch.setenv(HOSTED_ENV_VAR, value)
    assert is_hosted_multitenant() is True


@pytest.mark.parametrize("value", ["TRUE", "True", "Yes", "YES"])
def test_truthy_values_are_case_insensitive(monkeypatch, value):
    """Deliberate widening over the original heartbeat check.

    That one compared the raw stripped string, so `KILN_HOSTED_MULTITENANT=TRUE`
    read as FALSE — a plausible thing to write in a deploy config, and it
    would have silently un-suppressed the hosted heartbeat.  Pinned here so
    the widening is a decision on the record rather than an accident.
    """
    monkeypatch.setenv(HOSTED_ENV_VAR, value)
    assert is_hosted_multitenant() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe", " "])
def test_falsy_and_junk_values(monkeypatch, value):
    monkeypatch.setenv(HOSTED_ENV_VAR, value)
    assert is_hosted_multitenant() is False


def test_surrounding_whitespace_is_ignored(monkeypatch):
    """Deploy configs grow stray whitespace; it must not flip the meaning."""
    monkeypatch.setenv(HOSTED_ENV_VAR, "  1  ")
    assert is_hosted_multitenant() is True


def test_heartbeat_agrees_with_the_shared_predicate(monkeypatch):
    """The whole reason this module exists: no second copy to drift from.

    heartbeat._is_hosted_multitenant used to own its own env check.  If it
    ever grows one back, this fails.
    """
    from kiln.heartbeat import _is_hosted_multitenant

    monkeypatch.setenv(HOSTED_ENV_VAR, "TRUE")
    assert _is_hosted_multitenant() is is_hosted_multitenant() is True

    monkeypatch.delenv(HOSTED_ENV_VAR, raising=False)
    assert _is_hosted_multitenant() is is_hosted_multitenant() is False
