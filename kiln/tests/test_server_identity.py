"""What an app is told Kiln is called, and which picture to show for it."""

from __future__ import annotations

import asyncio

import pytest

from kiln import server, server_identity, startup_failure


def _server_info(mcp):
    """The identity block of a real initialize reply."""
    Client = pytest.importorskip("mcp").Client

    async def _connect():
        async with Client(mcp) as client:
            info = client.server_info
            return info.model_dump(by_alias=True, exclude_none=True)

    return asyncio.run(_connect())


def _assert_is_kiln(info: dict) -> None:
    assert info["name"] == "kiln"
    assert info["title"] == "Kiln"
    assert info["websiteUrl"] == "https://kiln3d.com"
    icons = info["icons"]
    assert icons, "no icon in the initialize reply"
    for icon in icons:
        assert icon["src"].startswith("https://kiln3d.com/")
        assert icon["mimeType"] in {"image/png", "image/svg+xml"}
        assert icon["sizes"]
    assert any(icon["mimeType"] == "image/png" for icon in icons)


def test_the_server_introduces_itself_as_kiln_with_its_logo():
    _assert_is_kiln(_server_info(server.mcp))


def test_the_recovery_server_carries_the_same_name_and_logo():
    diagnosis = startup_failure.explain(RuntimeError("boom"))
    _assert_is_kiln(_server_info(startup_failure.build_safe_mode_server(diagnosis, None)))


def test_no_display_name_says_kiln3d():
    assert "3d" not in server_identity.SERVER_TITLE.lower()


def test_a_server_class_without_the_fields_is_still_built():
    class Old:
        def __init__(self, name, instructions=None):
            self.name = name

    assert server_identity.identity_kwargs(Old) == {}
