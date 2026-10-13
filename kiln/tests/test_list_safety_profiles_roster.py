"""``list_safety_profiles`` names printers; it reads no printer's limits.

Since 2026-10-12 a catalogued printer's limits are one record per printer,
fetched through ``kiln._pro_catalogue_bridge`` the first time a door asks.
The list used to call ``get_profile`` for every id on the roster, which is
one catalogue fetch per printer on an install that has none of them -- and,
answered, a single call that hands back every ceiling Kiln holds.  It now
returns ids and names from the roster this install carries, and points at
``get_safety_profile`` for one printer's limits.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import kiln.safety_profiles as sp

_LIMIT_KEYS = {
    "max_hotend_temp",
    "max_bed_temp",
    "max_chamber_temp",
    "max_feedrate",
    "max_volumetric_flow",
    "build_volume",
    "limits_provenance",
}


def _tools() -> dict[str, Any]:
    from kiln.plugins.safety_tools import _SafetyToolsPlugin

    tools: dict[str, Any] = {}

    class FakeMCP:
        def tool(self_mcp, **_kwargs):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn

            return decorator

    _SafetyToolsPlugin().register(FakeMCP())
    return tools


@pytest.fixture
def listing(monkeypatch, tmp_path):
    """The tool, with this machine's overrides in a folder of the test's own,
    and every ask of the catalogue counted."""
    import kiln.server as srv
    from kiln import _pro_catalogue_bridge as bridge

    monkeypatch.setattr(srv, "_check_auth", lambda *_a, **_k: None)
    monkeypatch.setattr(sp, "_LOCAL_OVERRIDE_FILE", tmp_path / "local_printer_overrides.json")
    monkeypatch.setattr(sp, "_LEGACY_OVERRIDE_FILE", tmp_path / "community_profiles.json")
    monkeypatch.setattr(sp, "_LOCAL_DIR", tmp_path)
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    sp._local_overrides_loaded = False
    sp._local_override_cache.clear()
    asked: list[str] = []
    real = bridge.printer_record
    monkeypatch.setattr(bridge, "printer_record", lambda key: asked.append(key) or real(key))
    yield _tools()["list_safety_profiles"], asked
    sp._local_overrides_loaded = False
    sp._local_override_cache.clear()


def test_the_list_names_the_roster_and_carries_no_limit(listing):
    tool, _asked = listing
    out = tool()
    assert out["success"] is True and out["count"] == len(out["profiles"])
    by_id = {p["id"]: p for p in out["profiles"]}
    # A printer whose record is not held here is still listed, by its name.
    assert by_id["kilntest_served"]["display_name"] == "Kilntest Served 200"
    assert by_id["kilntest_open"]["display_name"] == "Kilntest Open 220"
    leaked = {key for p in out["profiles"] for key in p} & _LIMIT_KEYS
    assert not leaked, f"the list carries limits: {sorted(leaked)}"


def test_listing_asks_the_catalogue_for_nothing(listing):
    tool, asked = listing
    tool()
    assert asked == []


def test_the_answer_says_where_one_printers_limits_are(listing):
    tool, _asked = listing
    assert "get_safety_profile" in tool()["note"]


def test_a_printer_set_up_on_this_machine_is_listed_and_said_so(listing):
    tool, asked = listing
    sp._LOCAL_OVERRIDE_FILE.write_text(
        json.dumps(
            {
                "garage_build": {
                    "display_name": "Garage Build",
                    "max_hotend_temp": 230.0,
                    "max_bed_temp": 70.0,
                    "max_feedrate": 5000.0,
                    "build_volume": [200, 200, 200],
                }
            }
        ),
        encoding="utf-8",
    )
    sp._local_overrides_loaded = False
    sp._local_override_cache.clear()
    by_id = {p["id"]: p for p in tool()["profiles"]}
    assert by_id["garage_build"]["set_up_on_this_machine"] is True
    assert "set_up_on_this_machine" not in by_id["kilntest_open"]
    assert asked == []
