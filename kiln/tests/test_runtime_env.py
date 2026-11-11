"""The hosted-vs-local predicate — one answer, several callers.

Small module, but it decides two unrelated things: whether the heartbeat
reports this process as an install, and whether a user is told to go install
software.  Getting it wrong in the "hosted" direction means telling someone
to pip install onto a server they don't control; getting it wrong the other
way means the shared box reports itself as somebody's laptop.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest

import kiln.runtime_env as runtime_env
from kiln.runtime_env import HOSTED_ENV_VAR, is_hosted_multitenant


def test_absent_means_local(monkeypatch):
    """Unset is the safe reading: somebody's own install.

    A local install that wrongly believed it was hosted would hide its own
    fix-it instructions, which is the worst direction to be wrong in.
    """
    monkeypatch.delenv(HOSTED_ENV_VAR, raising=False)
    assert is_hosted_multitenant() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
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


class TestTheServersSwitch:
    """``hosted_multitenant()`` sets the answer for one block of work.

    Covers: the armed value wins over the env flag in both directions, the
    flag decides again once the block ends (also after a raise), nesting
    restores the outer value, and a worker started with ``asyncio.to_thread``
    inside the block reads the armed value.
    """

    def test_armed_with_the_flag_unset_reads_hosted(self, monkeypatch):
        monkeypatch.delenv(HOSTED_ENV_VAR, raising=False)
        with runtime_env.hosted_multitenant():
            assert is_hosted_multitenant() is True

    def test_armed_false_wins_over_the_flag(self, monkeypatch):
        monkeypatch.setenv(HOSTED_ENV_VAR, "1")
        with runtime_env.hosted_multitenant(False):
            assert is_hosted_multitenant() is False

    def test_the_flag_decides_again_after_the_block(self, monkeypatch):
        monkeypatch.delenv(HOSTED_ENV_VAR, raising=False)
        with runtime_env.hosted_multitenant():
            pass
        assert is_hosted_multitenant() is False

    def test_the_block_ends_when_it_raises(self, monkeypatch):
        monkeypatch.delenv(HOSTED_ENV_VAR, raising=False)
        with pytest.raises(RuntimeError), runtime_env.hosted_multitenant():
            raise RuntimeError("boom")
        assert is_hosted_multitenant() is False

    def test_nested_blocks_restore_the_outer_value(self, monkeypatch):
        monkeypatch.delenv(HOSTED_ENV_VAR, raising=False)
        with runtime_env.hosted_multitenant(True):
            with runtime_env.hosted_multitenant(False):
                assert is_hosted_multitenant() is False
            assert is_hosted_multitenant() is True

    def test_a_worker_thread_from_to_thread_reads_the_armed_value(self, monkeypatch):
        monkeypatch.delenv(HOSTED_ENV_VAR, raising=False)

        async def ask_from_a_worker() -> bool:
            with runtime_env.hosted_multitenant():
                return await asyncio.to_thread(is_hosted_multitenant)

        assert asyncio.run(ask_from_a_worker()) is True


#: Every reader that answers "is this the shared server?" for its own module.
_HOSTED_READERS = (
    ("kiln.heartbeat", "_is_hosted_multitenant"),
    ("kiln.update_nudge", "_hosted_multitenant"),
    ("kiln.community_outbox", "_hosted_aggregate_process"),
)


@pytest.mark.parametrize(("module", "name"), _HOSTED_READERS)
def test_every_hosted_reader_hears_the_switch(monkeypatch, module, name):
    read = getattr(importlib.import_module(module), name)
    monkeypatch.delenv(HOSTED_ENV_VAR, raising=False)
    assert read() is False
    with runtime_env.hosted_multitenant():
        assert read() is True


@pytest.mark.parametrize(("module", "name"), _HOSTED_READERS)
@pytest.mark.parametrize("value", ["1", "TRUE", "on", "0", ""])
def test_every_hosted_reader_reads_the_flag_alike(monkeypatch, module, name, value):
    read = getattr(importlib.import_module(module), name)
    monkeypatch.setenv(HOSTED_ENV_VAR, value)
    assert read() is is_hosted_multitenant()
