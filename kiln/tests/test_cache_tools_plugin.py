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


class TestMaterialFilterForgiveness:
    """The material filter is called by agents with arbitrary casing and
    padding; "pla" and "PLA " must mean PLA."""

    def test_filter_is_case_insensitive(self, registered_tools, cache, tmp_path) -> None:
        cache.add(_write_design(tmp_path, "bracket.stl", "bracket"), filament_type="PLA")

        for probe in ("PLA", "pla", "Pla"):
            result = registered_tools["list_cached_designs"](material=probe)
            assert result["success"] is True, result
            assert result["count"] == 1, f"material={probe!r} missed the PLA design"

    def test_filter_tolerates_padding(self, registered_tools, cache, tmp_path) -> None:
        cache.add(_write_design(tmp_path, "bracket.stl", "bracket"), filament_type="PLA")

        result = registered_tools["list_cached_designs"](material=" pla ")
        assert result["success"] is True, result
        assert result["count"] == 1

    def test_blank_material_means_no_filter(self, registered_tools, cache, tmp_path) -> None:
        cache.add(_write_design(tmp_path, "bracket.stl", "bracket"), filament_type="PLA")

        result = registered_tools["list_cached_designs"](material="   ")
        assert result["success"] is True, result
        assert result["count"] == 1


class TestRecacheUpdatesAnnotations:
    """Re-caching identical bytes dedups the file but must honor the new
    annotations -- success can never carry values the caller contradicted."""

    def test_new_material_and_label_stick(self, registered_tools, cache, tmp_path) -> None:
        path = _write_design(tmp_path, "bracket.stl", "bracket")
        first = registered_tools["cache_design"](path, label="v1", material="PLA")
        assert first["success"] is True, first

        second = registered_tools["cache_design"](path, label="v2", material="PETG")

        assert second["success"] is True, second
        assert second["cached_design"]["id"] == first["cached_design"]["id"]
        assert second["cached_design"]["filament_type"] == "PETG"
        assert second["cached_design"]["metadata"]["label"] == "v2"

        listed = registered_tools["list_cached_designs"](material="PETG")
        assert listed["count"] == 1, listed
        assert registered_tools["list_cached_designs"](material="PLA")["count"] == 0

    def test_omitted_fields_keep_stored_values(self, cache, tmp_path) -> None:
        path = _write_design(tmp_path, "bracket.stl", "bracket")
        cache.add(path, filament_type="PLA", tags=["calibration"], provider="meshy")

        again = cache.add(path, slicer_used="PrusaSlicer 2.7.1")

        assert again.filament_type == "PLA"
        assert again.tags == ["calibration"]
        assert again.provider == "meshy"
        assert again.slicer_used == "PrusaSlicer 2.7.1"

    def test_metadata_is_merged_not_replaced(self, cache, tmp_path) -> None:
        path = _write_design(tmp_path, "bracket.stl", "bracket")
        cache.add(path, metadata={"label": "v1", "infill_percent": 20})

        again = cache.add(path, metadata={"label": "v2"})

        assert again.metadata == {"label": "v2", "infill_percent": 20}
