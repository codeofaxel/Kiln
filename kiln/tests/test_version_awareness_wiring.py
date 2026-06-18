"""Wiring: the upgrade_kiln tool's confirm gate + the offer in the nudge block."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiln.plugins.utility_tools import _UtilityToolsPlugin


@pytest.fixture()
def upgrade_kiln():
    tools: dict = {}

    class MockMCP:
        def tool(self):
            def deco(fn):
                tools[fn.__name__] = fn
                return fn

            return deco

    _UtilityToolsPlugin().register(MockMCP())
    return tools["upgrade_kiln"]


def test_without_confirm_offers_and_touches_nothing(upgrade_kiln):
    with patch("kiln.self_update.perform_upgrade") as perform:
        out = upgrade_kiln(confirm=False)
        perform.assert_not_called()  # no install without explicit consent
    assert out["status"] == "needs_confirmation"
    assert "want me to update" in out["message"].lower()


def test_confirm_runs_the_upgrade_and_passes_result_through(upgrade_kiln):
    fake = {"ok": True, "status": "updated", "installed": "1.4.0", "message": "Updated."}
    with patch("kiln.self_update.perform_upgrade", return_value=fake) as perform:
        out = upgrade_kiln(confirm=True)
        perform.assert_called_once()
    assert out["success"] is True
    assert out["status"] == "updated"
    assert out["installed"] == "1.4.0"


def test_confirm_failure_surfaces_gracefully(upgrade_kiln):
    fake = {"ok": False, "status": "failed", "message": "managed env",
            "command": "pip install --upgrade kiln3d"}
    with patch("kiln.self_update.perform_upgrade", return_value=fake):
        out = upgrade_kiln(confirm=True)
    assert out["success"] is False
    assert out["status"] == "failed"


def test_force_is_threaded_to_perform_upgrade(upgrade_kiln):
    with patch("kiln.self_update.perform_upgrade", return_value={"ok": True}) as perform:
        upgrade_kiln(confirm=True, force=True)
        assert perform.call_args.kwargs.get("force") is True


# --- the nudge block now frames an offer + names the tool --------------------


def test_check_for_update_carries_offer_and_action_when_newer(monkeypatch):
    from kiln import version_check as vc

    monkeypatch.setattr(vc, "update_check_enabled", lambda: True)
    monkeypatch.setattr(vc, "_load_cache", lambda: {"latest": "9.9.9"})
    monkeypatch.setattr(vc, "_is_stale", lambda cache: False)

    block = vc.check_for_update(current_version="1.0.0")
    assert block is not None
    assert block["available"] is True
    assert block["action"] == "upgrade_kiln"
    assert block["offer"] and "newer kiln" in block["offer"].lower()
    assert block["command"].startswith("pip install --upgrade")
