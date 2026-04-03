"""Tests for kiln.plugins.version_tools — design version control tools."""

from __future__ import annotations

import pytest

from kiln.plugins.version_tools import _design_dir, _ensure_design_dir, _parse_version_ref

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _MockMcp:
    """Minimal MCP stub that captures registered tools by name."""

    def __init__(self) -> None:
        self._tools: dict = {}

    def tool(self):
        def decorator(fn):
            self._tools[fn.__name__] = fn
            return fn

        return decorator

    def __getitem__(self, name: str):
        return self._tools[name]


def _make_mcp_with_tools():
    """Instantiate the plugin and return a mock MCP with all tools registered."""
    from kiln.plugins.version_tools import _VersionToolsPlugin

    mcp = _MockMcp()
    _VersionToolsPlugin().register(mcp)
    return mcp


# ---------------------------------------------------------------------------
# TestParseVersionRef
# ---------------------------------------------------------------------------


class TestParseVersionRef:
    """_parse_version_ref: parses 'design_id:N' and plain integer refs."""

    def test_design_id_colon_version(self):
        result = _parse_version_ref("my-coaster:3")
        assert result == ("my-coaster", 3)

    def test_plain_int_with_default(self):
        result = _parse_version_ref("5", default_design_id="foo")
        assert result == ("foo", 5)

    def test_plain_int_no_default_raises(self):
        with pytest.raises(ValueError, match="design_id:N"):
            _parse_version_ref("5")

    def test_invalid_version_raises(self):
        with pytest.raises(ValueError, match="Invalid version number"):
            _parse_version_ref("foo:abc")

    def test_whitespace_stripped(self):
        result = _parse_version_ref(" foo : 3 ")
        assert result == ("foo", 3)


# ---------------------------------------------------------------------------
# TestDesignDir
# ---------------------------------------------------------------------------


class TestDesignDir:
    """_design_dir and _ensure_design_dir: path computation and creation."""

    def test_design_dir_path(self, monkeypatch, tmp_path):
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        result = _design_dir("my-coaster")
        # _design_dir uses os.path.expanduser on _DESIGNS_ROOT/design_id
        # since tmp_path has no ~, the path should end with my-coaster
        assert result.endswith("my-coaster")
        assert str(tmp_path) in result

    def test_ensure_creates_directory(self, monkeypatch, tmp_path):
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        path = _ensure_design_dir("new-design")
        import os

        assert os.path.isdir(path)
        assert path.endswith("new-design")


# ---------------------------------------------------------------------------
# TestSaveDesignVersion
# ---------------------------------------------------------------------------


class TestSaveDesignVersion:
    """save_design_version MCP tool: create, increment, pro fallback."""

    def test_first_version_creates_recipe(self, monkeypatch, tmp_path):
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        mcp = _make_mcp_with_tools()
        result = mcp["save_design_version"](
            design_id="test-coaster",
            scad_source="cube([80, 80, 3]);",
            prompt="flat coaster",
        )
        assert result["ok"] is True
        ver = result["version"]
        assert ver["version"] == 1
        assert ver["design_id"] == "test-coaster"
        assert ver["diff_from_prev"] is None
        assert ver["parent_version"] is None

    def test_second_version_increments(self, monkeypatch, tmp_path):
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        mcp = _make_mcp_with_tools()
        mcp["save_design_version"](
            design_id="test-coaster",
            scad_source="cube([80, 80, 3]);",
        )
        result = mcp["save_design_version"](
            design_id="test-coaster",
            scad_source="cube([90, 90, 3]);",
            notes="bigger coaster",
        )
        assert result["ok"] is True
        ver = result["version"]
        assert ver["version"] == 2
        assert ver["parent_version"] is not None
        # diff should capture the source change
        assert ver["diff_from_prev"] is not None
        assert "80" in ver["diff_from_prev"] or "90" in ver["diff_from_prev"]

    def test_pro_enrichment_skipped_without_kiln_pro(self, monkeypatch, tmp_path):
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        mcp = _make_mcp_with_tools()
        # Should not raise even though kiln_pro is absent; result is still ok
        result = mcp["save_design_version"](
            design_id="pro-test",
            scad_source="sphere(r=10);",
            stl_path="/nonexistent/output.stl",
            provenance={"tools_used": ["openscad"]},
        )
        assert result["ok"] is True
        # No crash from missing kiln_pro
        assert "version" in result


# ---------------------------------------------------------------------------
# TestListDesignVersions
# ---------------------------------------------------------------------------


class TestListDesignVersions:
    """list_design_versions MCP tool: history listing, newest first."""

    def test_list_returns_versions_newest_first(self, monkeypatch, tmp_path):
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        mcp = _make_mcp_with_tools()
        for i in range(1, 4):
            mcp["save_design_version"](
                design_id="coaster",
                scad_source=f"cube([{i * 10}, {i * 10}, 3]);",
            )
        result = mcp["list_design_versions"](design_id="coaster")
        assert result["ok"] is True
        assert result["count"] == 3
        versions = result["versions"]
        # Newest first means version numbers descend
        version_nums = [v["version"] for v in versions]
        assert version_nums == sorted(version_nums, reverse=True)

    def test_list_empty_design(self, monkeypatch, tmp_path):
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        mcp = _make_mcp_with_tools()
        result = mcp["list_design_versions"](design_id="ghost-design")
        assert result["ok"] is True
        assert result["count"] == 0
        assert result["versions"] == []


# ---------------------------------------------------------------------------
# TestDiffDesignVersions
# ---------------------------------------------------------------------------


class TestDiffDesignVersions:
    """diff_design_versions MCP tool: unified diff between two versions."""

    def test_diff_shows_scad_changes(self, monkeypatch, tmp_path):
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        mcp = _make_mcp_with_tools()
        mcp["save_design_version"](
            design_id="diff-coaster",
            scad_source="cube([80, 80, 3]);",
        )
        mcp["save_design_version"](
            design_id="diff-coaster",
            scad_source="cylinder(r=40, h=3);",
        )
        result = mcp["diff_design_versions"](
            version_id_a="diff-coaster:1",
            version_id_b="diff-coaster:2",
        )
        assert result["ok"] is True
        diff = result["diff"]
        assert "cube" in diff or "cylinder" in diff


# ---------------------------------------------------------------------------
# TestRollbackDesignVersion
# ---------------------------------------------------------------------------


class TestRollbackDesignVersion:
    """rollback_design_version MCP tool: creates new version from old source."""

    def test_rollback_creates_new_version_with_old_source(self, monkeypatch, tmp_path):
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        mcp = _make_mcp_with_tools()
        # v1 with source A
        mcp["save_design_version"](
            design_id="rb-coaster",
            scad_source="cube([80, 80, 3]); // v1",
        )
        # v2 with source B
        mcp["save_design_version"](
            design_id="rb-coaster",
            scad_source="sphere(r=40); // v2",
        )
        # rollback to v1
        result = mcp["rollback_design_version"](
            design_id="rb-coaster",
            to_version_id="1",
        )
        assert result["ok"] is True
        ver = result["version"]
        # A new version should be created (v3)
        assert ver["version"] == 3
        assert ver["restored_from_version"] == 1
        # Verify the rolled-back recipe has v1's source
        get_result = mcp["get_design_version"](version_id="rb-coaster:3")
        assert get_result["ok"] is True
        assert "v1" in (get_result["version"]["source_scad"] or "")


# ---------------------------------------------------------------------------
# TestGetDesignVersion
# ---------------------------------------------------------------------------


class TestGetDesignVersion:
    """get_design_version MCP tool: retrieve by version_id."""

    def test_get_existing_version(self, monkeypatch, tmp_path):
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        mcp = _make_mcp_with_tools()
        mcp["save_design_version"](
            design_id="fetch-coaster",
            scad_source="cube([80, 80, 3]);",
            prompt="a flat coaster",
            notes="initial",
        )
        result = mcp["get_design_version"](version_id="fetch-coaster:1")
        assert result["ok"] is True
        ver = result["version"]
        assert ver["version"] == 1
        assert ver["source_scad"] == "cube([80, 80, 3]);"
        assert ver["prompt"] == "a flat coaster"
        assert ver["notes"] == "initial"

    def test_get_nonexistent_version_returns_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        mcp = _make_mcp_with_tools()
        result = mcp["get_design_version"](version_id="foo:999")
        assert result["ok"] is False
        assert "error" in result


# ---------------------------------------------------------------------------
# TestSearchDesignVersions
# ---------------------------------------------------------------------------


class TestSearchDesignVersions:
    """search_design_versions MCP tool: substring search across all designs."""

    def test_search_finds_by_prompt(self, monkeypatch, tmp_path):
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        mcp = _make_mcp_with_tools()
        mcp["save_design_version"](
            design_id="ash-coaster",
            scad_source="cube([80, 80, 3]);",
            prompt="ash cat portrait coaster",
        )
        mcp["save_design_version"](
            design_id="generic-coaster",
            scad_source="cube([80, 80, 3]);",
            prompt="plain simple coaster",
        )
        result = mcp["search_design_versions"](query="ash")
        assert result["ok"] is True
        assert result["count"] >= 1
        found_ids = [v["design_id"] for v in result["versions"]]
        assert "ash-coaster" in found_ids
        assert "generic-coaster" not in found_ids

    def test_search_no_results(self, monkeypatch, tmp_path):
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path))
        mcp = _make_mcp_with_tools()
        mcp["save_design_version"](
            design_id="some-coaster",
            scad_source="cube([80, 80, 3]);",
            prompt="simple flat coaster",
        )
        result = mcp["search_design_versions"](query="zzzzz")
        assert result["ok"] is True
        assert result["count"] == 0
        assert result["versions"] == []
