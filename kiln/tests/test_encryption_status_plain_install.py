"""The lockable-profiles tools on an install without the Enterprise package.

``encryption_status`` imports the encryption module, which is not part of
a plain install.  It used to answer ``INTERNAL_ERROR`` ("No module named
...") at every tier, so a person could not even read whether their files were
encrypted.  Encryption at rest
is applied by the machine that stores the files, so the honest answer here
is local and certain: it is not active on this machine.  These tests pin:

* the door answers instead of refusing, with the same ``encryption`` keys
  the full answer carries, each one true of this machine;
* a key set in the environment is reported, and the sentence says nothing
  here uses it;
* the answer names no internal module;
* the Enterprise tier gate in front of the door is unchanged;
* ``lock_safety_profile`` locks and is enforced with nothing beyond a plain
  install.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

# The keys of the full answer's ``encryption`` block.
_STATUS_KEYS = {"available", "key_configured", "library_installed", "supports_rotation"}


class _FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


@pytest.fixture
def plain_install(monkeypatch, tmp_path):
    """The enterprise tools registered as on a plain install, signed in on
    the plan the test names."""
    import kiln.account_plan as account_plan
    import kiln.safety_profiles as sp
    import kiln.server as srv
    from kiln.plugins.enterprise_tools import _EnterpriseToolsPlugin

    monkeypatch.setitem(sys.modules, "kiln.gcode_encryption", None)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    monkeypatch.delenv("KILN_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(srv, "_check_auth", lambda scope: None)
    monkeypatch.setattr(sp, "_LOCAL_DIR", tmp_path)
    monkeypatch.setattr(sp, "_LOCK_FILE", tmp_path / "locked_profiles.json")
    monkeypatch.setattr(sp, "_locked_profiles", set())
    monkeypatch.setattr(sp, "_locks_loaded", False)

    def tools(*, enterprise: bool) -> dict[str, Any]:
        monkeypatch.setattr(account_plan, "_covers", lambda required: enterprise)
        mcp = _FakeMCP()
        _EnterpriseToolsPlugin().register(mcp)
        return mcp.tools

    return tools


def test_encryption_status_answers_instead_of_refusing(plain_install):
    out = plain_install(enterprise=True)["encryption_status"]()

    assert out["success"] is True, out
    assert set(out["encryption"]) == _STATUS_KEYS
    assert out["encryption"]["available"] is False
    assert out["encryption"]["key_configured"] is False
    assert out["encryption"]["supports_rotation"] is False
    assert isinstance(out["encryption"]["library_installed"], bool)
    assert "not installed on this machine" in out["message"]
    assert "unencrypted" in out["message"]
    assert "kiln3d.com/pricing" in out["message"]


def test_a_key_in_the_environment_is_reported_and_said_to_be_unused(plain_install, monkeypatch):
    monkeypatch.setenv("KILN_ENCRYPTION_KEY", "a passphrase")
    out = plain_install(enterprise=True)["encryption_status"]()

    assert out["success"] is True
    assert out["encryption"]["key_configured"] is True
    assert out["encryption"]["available"] is False
    assert "KILN_ENCRYPTION_KEY is set, but nothing on this machine uses it" in out["message"]
    assert "a passphrase" not in str(out)


def test_the_answer_names_no_internal_module(plain_install, monkeypatch):
    monkeypatch.setenv("KILN_ENCRYPTION_KEY", "x")
    text = str(plain_install(enterprise=True)["encryption_status"]()).lower()

    for name in ("kiln_pro", "kiln-pro", "gcode_encryption", "no module named"):
        assert name not in text


def test_the_enterprise_gate_still_stands(plain_install):
    out = plain_install(enterprise=False)["encryption_status"]()

    assert out["success"] is False
    assert out["required_tier"] == "enterprise"


def test_lock_safety_profile_works_on_a_plain_install(plain_install):
    import kiln.safety_profiles as sp

    tools = plain_install(enterprise=True)
    out = tools["lock_safety_profile"]("ender3")

    assert out["success"] is True, out
    assert sp.is_profile_locked("ender3")
    assert sp._LOCK_FILE.exists()
    assert tools["unlock_safety_profile"]("ender3")["success"] is True
    assert not sp.is_profile_locked("ender3")
