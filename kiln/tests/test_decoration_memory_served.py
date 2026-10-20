"""A saved decoration's memory reaches a customer, worked out on Kiln's servers.

The memory of a saved decoration -- recording what printed well, replaying
it, versions, history and rollback -- is Kiln Pro.  Its four tools used to
be registered only by the private package, so no customer could run them;
they are now this package's own tools, and the change each one makes comes
from Kiln's servers (``kiln._pro_decoration_bridge``) and is written into the
decoration's own folder here.

These cover this side, with only Kiln's servers stood in for: the tools
exist, what leaves this computer, that a plan is checked before anything is
written, that a miss changes nothing, and that applying a decoration on a
plan without decoration memory uses the standard settings and says so.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiln import _pro_decoration_bridge as bridge
from kiln.decoration_library import (
    get_decoration,
    get_library_dir,
    record_decoration_success,
    save_decoration,
)

MEMORY_TOOLS = (
    "record_decoration_success",
    "iterate_decoration",
    "decoration_history",
    "rollback_decoration",
)


@pytest.fixture(autouse=True)
def _library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KILN_DECORATIONS_DIR", str(tmp_path / "decorations"))


class _Servers:
    """Kiln's servers, answering with whatever the test sets."""

    def __init__(self) -> None:
        self.answer: dict[str, Any] = {}
        self.sent: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []

    def __call__(self, tool_name, /, _timeout=None, _asked_by_user=True, **kwargs):
        assert tool_name == bridge.WIRE_TOOL
        self.sent.append(kwargs)
        self.calls.append({"timeout": _timeout, "asked_by_user": _asked_by_user})
        return self.answer


@pytest.fixture
def servers(monkeypatch: pytest.MonkeyPatch) -> _Servers:
    import kiln.server as ks

    door = _Servers()
    monkeypatch.setattr(ks, "_pro_api_call", door)
    return door


def _tools() -> dict[str, Any]:
    from kiln.plugins.decoration_library_tools import plugin

    found: dict[str, Any] = {}

    class _MCP:
        def tool(self, *_a, **_k):
            def keep(fn):
                found[fn.__name__] = fn
                return fn

            return keep

    plugin.register(_MCP())
    return found


def _saved_qr() -> Path:
    save_decoration(
        "Shop QR", content_type="qr", content_data="https://shop.example/secret-sale",
        tags=["client:acme"], depth_mm=0.5, material="PLA",
    )
    return get_library_dir() / "shop-qr"


def test_the_four_memory_tools_are_this_packages_own():
    tools = _tools()
    for name in MEMORY_TOOLS:
        assert name in tools, f"{name} is not registered on a plain install"


def test_only_version_numbers_and_print_counts_leave_this_computer(servers):
    folder = _saved_qr()
    servers.answer = {"success": True, "status": "ok", "op": "record",
                      "plan": {"base": "manifest.json", "set": {"print_count": 1}}}
    record_decoration_success("shop-qr", material="PLA", depth_mm=0.5)
    sent = servers.sent[0]
    record = sent["records"]["manifest.json"]
    assert set(record) <= {
        "version", "created", "changes", "parent_version", "proven_settings",
        "print_count", "counted_jobs", "content_type",
    }
    assert "shop.example" not in json.dumps(sent) and "acme" not in json.dumps(sent)
    assert json.loads((folder / "manifest.json").read_text())["print_count"] == 1


@pytest.mark.parametrize(
    "plan",
    [
        {"base": "../../elsewhere.json", "set": {}},
        {"base": "manifest.json", "archive": [{"as": "../escape.json"}], "set": {}},
        {"base": "manifest.json", "set": {"content_data": "planted"}},
        {"base": "manifest.json", "set": {"name": "renamed"}},
        {"base": "manifest.json", "set": {}, "copy_content_as": "../../evil.sh"},
        "not a plan",
    ],
)
def test_a_plan_is_checked_whole_and_a_bad_one_writes_nothing(servers, plan):
    folder = _saved_qr()
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    servers.answer = {"success": True, "status": "ok", "op": "record", "plan": plan}
    with pytest.raises(ValueError):
        record_decoration_success("shop-qr", material="PLA", depth_mm=0.5)
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before


def test_a_miss_changes_nothing_and_the_tool_says_so(servers):
    folder = _saved_qr()
    before = (folder / "manifest.json").read_bytes()
    servers.answer = {"status": "error", "code": "KILN_ACCOUNT_NOT_PAIRED",
                      "error": "Signing in is free.", "why": "signed_out"}
    answer = _tools()["record_decoration_success"](name="shop-qr", material="PLA", depth_mm=0.5)
    assert answer["status"] == "error" and answer["tool"] == "record_decoration_success"
    assert "Nothing in the decoration's folder changed" in answer["error"]
    assert answer["why"] == "signed_out"
    assert (folder / "manifest.json").read_bytes() == before


def test_a_plan_without_decoration_memory_gets_the_servers_own_answer(servers):
    _saved_qr()
    servers.answer = {"status": "error", "success": False, "code": "TIER_REQUIRED",
                      "error": "Decoration memory needs Kiln Pro.", "required_tier": "pro",
                      "upgrade_url": "https://kiln3d.com/pricing"}
    answer = _tools()["iterate_decoration"](name="shop-qr", depth_mm=0.8)
    assert answer["code"] == "TIER_REQUIRED" and answer["tool"] == "iterate_decoration"
    assert answer["upgrade_url"].startswith("https://kiln3d.com/")
    assert not (get_library_dir() / "shop-qr" / "manifest.v1.json").exists()


def test_applying_without_decoration_memory_uses_the_standard_settings_and_says_so(servers, tmp_path, monkeypatch):
    folder = _saved_qr()
    data = json.loads((folder / "manifest.json").read_text())
    data["proven_settings"]["PLA"]["depth_mm"] = 0.9
    data["proven_settings"]["PLA"]["success_count"] = 3
    (folder / "manifest.json").write_text(json.dumps(data))
    servers.answer = {"status": "error", "success": False, "code": "TIER_REQUIRED",
                      "error": "Decoration memory needs Kiln Pro.",
                      "upgrade_url": "https://kiln3d.com/pricing"}
    model = tmp_path / "coaster.stl"
    model.write_text("solid x\nendsolid x\n")
    seen: dict[str, Any] = {}

    def decorate(**kwargs):
        seen.update(kwargs)
        return {"success": True}

    import kiln.server as ks

    monkeypatch.setattr(ks, "decorate_surface", decorate, raising=False)
    result = _tools()["apply_decoration"](name="shop-qr", model_path=str(model), material="PLA")
    assert seen["depth_mm"] == 0.5, "the standard QR depth, not the one that printed well"
    assert result["settings_source"] == "default"
    assert "Kiln Pro replays" in result["settings_note"]
    assert servers.calls[0]["asked_by_user"] is False


def test_a_decoration_that_never_printed_well_asks_nobody(servers):
    _saved_qr()
    from kiln.decoration_library import resolve_decoration_settings

    data = json.loads((get_library_dir() / "shop-qr" / "manifest.json").read_text())
    assert data["proven_settings"]["PLA"]["success_count"] == 0
    settings = resolve_decoration_settings(get_decoration("shop-qr"), material="PLA")
    assert settings["source"] == "default"
    assert servers.sent == []


def test_a_change_nobody_waits_on_does_not_wait_long(servers):
    _saved_qr()
    servers.answer = {"success": True, "status": "ok", "op": "record",
                      "plan": {"base": "manifest.json", "set": {"print_count": 1}}}
    record_decoration_success("shop-qr", material="PLA", depth_mm=0.5, asked_by_user=False)
    assert servers.calls[0] == {"timeout": bridge._BACKGROUND_TIMEOUT_S, "asked_by_user": False}
