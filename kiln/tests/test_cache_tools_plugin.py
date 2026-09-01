"""Tests for the design-cache half of kiln.plugins.cache_tools.

``list_cached_designs`` and ``cache_design`` were written against a
``DesignCache`` API that never existed -- ``list_designs()`` and
``add(label=..., material=...)``.  Both raised inside the tool's blanket
``except Exception`` and came back as ``CACHE_ERROR``, so neither tool
had ever returned a design.  These tests exercise the tools end to end
against a real cache so the wiring cannot silently rot again.
"""

from __future__ import annotations

import time
from unittest import mock

import pytest

from kiln.design_cache import DesignCache


@pytest.fixture()
def registered_tools():
    """Register the cache plugin against a mock MCP server."""
    tools: dict[str, callable] = {}

    class MockMCP:
        def tool(self):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn

            return decorator

    from kiln.plugins.cache_tools import plugin

    plugin.register(MockMCP())
    return tools


@pytest.fixture()
def cache(tmp_path, monkeypatch):
    """Point ``get_design_cache()`` at a throwaway cache directory."""
    instance = DesignCache(cache_dir=str(tmp_path / "designs"))
    monkeypatch.setattr("kiln.design_cache._design_cache", instance)
    yield instance
    instance.close()


def _write_design(tmp_path, name: str, body: str) -> str:
    """Write a design file with unique content (identical bytes dedup)."""
    path = tmp_path / name
    path.write_text(body)
    return str(path)


class TestListCachedDesigns:
    def test_lists_cached_designs(self, registered_tools, cache, tmp_path) -> None:
        cache.add(_write_design(tmp_path, "bracket.stl", "bracket"), filament_type="PLA")
        cache.add(_write_design(tmp_path, "spacer.stl", "spacer"), filament_type="PETG")

        result = registered_tools["list_cached_designs"]()

        assert result["success"] is True, result
        assert result["count"] == 2
        assert {d["file_name"] for d in result["designs"]} == {"bracket.stl", "spacer.stl"}

    def test_filters_by_material(self, registered_tools, cache, tmp_path) -> None:
        cache.add(_write_design(tmp_path, "bracket.stl", "bracket"), filament_type="PLA")
        cache.add(_write_design(tmp_path, "spacer.stl", "spacer"), filament_type="PETG")

        result = registered_tools["list_cached_designs"](material="PETG")

        assert result["success"] is True, result
        assert result["count"] == 1
        assert result["designs"][0]["file_name"] == "spacer.stl"
        assert result["designs"][0]["filament_type"] == "PETG"

    def test_honours_limit(self, registered_tools, cache, tmp_path) -> None:
        for i in range(3):
            cache.add(_write_design(tmp_path, f"part{i}.stl", f"part {i}"), filament_type="PLA")

        result = registered_tools["list_cached_designs"](limit=2)

        assert result["success"] is True, result
        assert result["count"] == 2
        assert len(result["designs"]) == 2

    def test_most_recently_used_first(self, registered_tools, cache, tmp_path) -> None:
        first = cache.add(_write_design(tmp_path, "bracket.stl", "bracket"), filament_type="PLA")
        cache.add(_write_design(tmp_path, "spacer.stl", "spacer"), filament_type="PLA")

        time.sleep(0.01)
        cache.record_use(first.id)

        result = registered_tools["list_cached_designs"]()

        assert result["success"] is True, result
        assert [d["file_name"] for d in result["designs"]] == ["bracket.stl", "spacer.stl"]

    def test_empty_cache_is_a_success_not_an_error(self, registered_tools, cache) -> None:
        result = registered_tools["list_cached_designs"]()

        assert result["success"] is True, result
        assert result["count"] == 0
        assert result["designs"] == []


class TestCacheDesign:
    def test_material_survives_the_round_trip(self, registered_tools, cache, tmp_path) -> None:
        """cache_design(material=...) must be findable by list_cached_designs(material=...)."""
        path = _write_design(tmp_path, "bracket.stl", "bracket")

        cached = registered_tools["cache_design"](path, label="corner bracket", material="PETG")

        assert cached["success"] is True, cached
        assert cached["cached_design"]["filament_type"] == "PETG"
        assert cached["cached_design"]["metadata"]["label"] == "corner bracket"

        listed = registered_tools["list_cached_designs"](material="PETG")
        assert listed["success"] is True, listed
        assert [d["file_name"] for d in listed["designs"]] == ["bracket.stl"]


class TestAuthGate:
    """The design-cache reads sit behind the same "cache" scope as their six
    siblings.  They return absolute ``file_path`` values plus ``scad_source``
    and ``generation_prompt`` -- the user's own design DNA -- so an auth-enabled
    deployment must not serve them unauthenticated.
    """

    DENIED = {"success": False, "error": {"code": "AUTH_ERROR", "message": "nope"}}

    def test_list_cached_designs_refuses_when_auth_denies(self, registered_tools, cache, tmp_path) -> None:
        cache.add(_write_design(tmp_path, "secret.stl", "secret"), filament_type="PLA")

        with mock.patch("kiln.server._check_auth", return_value=self.DENIED):
            result = registered_tools["list_cached_designs"]()

        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_ERROR"
        assert "designs" not in result

    def test_get_cached_design_refuses_when_auth_denies(self, registered_tools, cache, tmp_path) -> None:
        entry = cache.add(_write_design(tmp_path, "secret.stl", "secret"), filament_type="PLA")

        with mock.patch("kiln.server._check_auth", return_value=self.DENIED):
            result = registered_tools["get_cached_design"](entry.id)

        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_ERROR"
        assert "design" not in result

    def test_reads_still_work_when_auth_allows(self, registered_tools, cache, tmp_path) -> None:
        """The gate must not break the ungated (auth-disabled) default."""
        entry = cache.add(_write_design(tmp_path, "bracket.stl", "bracket"), filament_type="PLA")

        with mock.patch("kiln.server._check_auth", return_value=None):
            listed = registered_tools["list_cached_designs"]()
            fetched = registered_tools["get_cached_design"](entry.id)

        assert listed["success"] is True, listed
        assert listed["count"] == 1
        assert fetched["success"] is True, fetched
        assert fetched["design"]["id"] == entry.id
