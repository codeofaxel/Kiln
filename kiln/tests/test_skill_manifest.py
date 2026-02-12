"""Tests for skill manifest generation and distribution.

Covers:
    - SkillManifest dataclass defaults and to_dict serialization
    - generate_manifest() populates version and tool_count
    - get_version() returns a string
    - get_tool_count() reads from tool_safety.json
    - get_skill_definition_path() finds SKILL.md in various locations
    - get_skill_definition_path() raises FileNotFoundError when missing
    - detect_agent_workspaces() discovers marker files
    - detect_agent_workspaces() deduplicates results
    - detect_agent_workspaces() handles empty directories
    - _marker_to_agent_type() mapping
    - _check_skill_installed() detection
    - install_skill() copies SKILL.md to workspace root
    - install_skill() prefers .dev/ directory when present
    - install_skill() refuses overwrite without --force
    - install_skill() overwrites with force=True
    - install_skill() handles missing workspace
    - install_skill() handles missing SKILL.md source
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from kiln.skill_manifest import (
    SkillManifest,
    _check_skill_installed,
    _marker_to_agent_type,
    detect_agent_workspaces,
    generate_manifest,
    get_skill_definition_path,
    get_tool_count,
    get_version,
    install_skill,
)


# ===================================================================
# SkillManifest dataclass
# ===================================================================


class TestSkillManifest:
    """Verify SkillManifest fields, defaults, and serialization."""

    def test_defaults(self):
        m = SkillManifest()
        assert m.name == "kiln"
        assert m.version == ""
        assert m.description == "3D printer control and monitoring via CLI and MCP"
        assert "KILN_PRINTER_HOST" in m.required_env
        assert "KILN_PRINTER_API_KEY" in m.required_env
        assert "KILN_PRINTER_TYPE" in m.required_env
        assert "cli" in m.interfaces
        assert "mcp" in m.interfaces
        assert m.tool_count == 0
        assert "safe" in m.safety_levels
        assert m.setup_command == "kiln verify"
        assert m.health_command == "kiln status --json"

    def test_to_dict_returns_dict(self):
        m = SkillManifest(version="1.0.0", tool_count=42)
        d = m.to_dict()
        assert isinstance(d, dict)
        assert d["name"] == "kiln"
        assert d["version"] == "1.0.0"
        assert d["tool_count"] == 42

    def test_to_dict_all_keys_present(self):
        d = SkillManifest().to_dict()
        expected_keys = {
            "name", "version", "description",
            "required_env", "optional_env",
            "interfaces", "tool_count", "safety_levels",
            "setup_command", "health_command",
        }
        assert set(d.keys()) == expected_keys

    def test_to_dict_lists_are_plain_lists(self):
        d = SkillManifest().to_dict()
        assert isinstance(d["required_env"], list)
        assert isinstance(d["optional_env"], list)
        assert isinstance(d["interfaces"], list)
        assert isinstance(d["safety_levels"], list)


# ===================================================================
# Version and tool count helpers
# ===================================================================


class TestGetVersion:
    """Verify get_version returns a string."""

    def test_returns_string(self):
        v = get_version()
        assert isinstance(v, str)
        assert len(v) > 0

    def test_returns_unknown_on_import_error(self):
        with patch(
            "importlib.metadata.version",
            side_effect=ImportError("no package"),
        ):
            result = get_version()
            assert result == "unknown"


class TestGetToolCount:
    """Verify get_tool_count reads from tool_safety.json."""

    def test_returns_positive_int(self):
        count = get_tool_count()
        assert isinstance(count, int)
        assert count > 0  # we know tool_safety.json has entries

    def test_returns_zero_on_json_decode_error(self, tmp_path: Path):
        bad_json = tmp_path / "data" / "tool_safety.json"
        bad_json.parent.mkdir(parents=True)
        bad_json.write_text("not valid json")
        import kiln.skill_manifest as mod
        original_file = mod.__file__
        try:
            # Point __file__ so the data path resolves to our bad JSON
            mod.__file__ = str(tmp_path / "skill_manifest.py")
            assert get_tool_count() == 0
        finally:
            mod.__file__ = original_file


# ===================================================================
# generate_manifest
# ===================================================================


class TestGenerateManifest:
    """Verify generate_manifest produces a complete manifest."""

    def test_populates_version(self):
        m = generate_manifest()
        assert m.version != ""

    def test_populates_tool_count(self):
        m = generate_manifest()
        assert m.tool_count > 0

    def test_returns_skill_manifest_instance(self):
        m = generate_manifest()
        assert isinstance(m, SkillManifest)

    def test_to_dict_roundtrip(self):
        m = generate_manifest()
        d = m.to_dict()
        assert d["name"] == "kiln"
        assert isinstance(d["tool_count"], int)


# ===================================================================
# get_skill_definition_path
# ===================================================================


class TestGetSkillDefinitionPath:
    """Verify SKILL.md lookup across candidate locations."""

    def test_finds_dev_skill_md(self):
        """Should find .dev/SKILL.md in the repo."""
        # This test depends on the repo having .dev/SKILL.md
        try:
            p = get_skill_definition_path()
            assert p.is_file()
            assert p.name == "SKILL.md"
        except FileNotFoundError:
            pytest.skip("SKILL.md not available in test environment")

    def test_raises_when_not_found(self, tmp_path: Path):
        """Should raise FileNotFoundError when no candidate exists."""
        import kiln.skill_manifest as mod

        # Point __file__ to a fake location so all candidates miss
        fake_file = tmp_path / "fake" / "kiln" / "skill_manifest.py"
        fake_file.parent.mkdir(parents=True)
        original_file = mod.__file__
        try:
            mod.__file__ = str(fake_file)
            with patch("kiln.skill_manifest.Path.home", return_value=tmp_path / "fakehome"):
                with pytest.raises(FileNotFoundError, match="SKILL.md not found"):
                    get_skill_definition_path()
        finally:
            mod.__file__ = original_file


# ===================================================================
# detect_agent_workspaces
# ===================================================================


class TestDetectAgentWorkspaces:
    """Verify workspace detection via marker files."""

    def test_finds_claude_code_workspace(self, tmp_path: Path):
        ws = tmp_path / "myproject"
        ws.mkdir()
        (ws / "CLAUDE.md").write_text("# Claude")
        result = detect_agent_workspaces(search_dir=str(tmp_path))
        assert len(result) == 1
        assert result[0]["agent_type"] == "claude_code"
        assert result[0]["marker"] == "CLAUDE.md"
        assert result[0]["skill_installed"] is False

    def test_finds_cursor_workspace(self, tmp_path: Path):
        ws = tmp_path / "cursorproject"
        ws.mkdir()
        (ws / ".cursorrules").write_text("")
        result = detect_agent_workspaces(search_dir=str(tmp_path))
        assert len(result) == 1
        assert result[0]["agent_type"] == "cursor"

    def test_finds_windsurf_workspace(self, tmp_path: Path):
        ws = tmp_path / "wsproject"
        ws.mkdir()
        (ws / ".windsurfrules").write_text("")
        result = detect_agent_workspaces(search_dir=str(tmp_path))
        assert len(result) == 1
        assert result[0]["agent_type"] == "windsurf"

    def test_finds_workspace_in_base_dir(self, tmp_path: Path):
        """Marker in the search_dir root itself should be detected."""
        (tmp_path / "CLAUDE.md").write_text("# Claude")
        result = detect_agent_workspaces(search_dir=str(tmp_path))
        assert len(result) >= 1
        paths = [r["path"] for r in result]
        assert str(tmp_path) in paths

    def test_empty_directory(self, tmp_path: Path):
        result = detect_agent_workspaces(search_dir=str(tmp_path))
        assert result == []

    def test_deduplicates_results(self, tmp_path: Path):
        """Same workspace should not appear twice."""
        (tmp_path / "CLAUDE.md").write_text("# Claude")
        result = detect_agent_workspaces(search_dir=str(tmp_path))
        paths = [r["path"] for r in result]
        assert len(paths) == len(set(paths))

    def test_detects_skill_installed(self, tmp_path: Path):
        ws = tmp_path / "project"
        ws.mkdir()
        (ws / "CLAUDE.md").write_text("# Claude")
        (ws / "SKILL.md").write_text("# Skill")
        result = detect_agent_workspaces(search_dir=str(tmp_path))
        assert len(result) == 1
        assert result[0]["skill_installed"] is True

    def test_detects_skill_in_dev_dir(self, tmp_path: Path):
        ws = tmp_path / "project"
        ws.mkdir()
        (ws / "CLAUDE.md").write_text("# Claude")
        dev = ws / ".dev"
        dev.mkdir()
        (dev / "SKILL.md").write_text("# Skill")
        result = detect_agent_workspaces(search_dir=str(tmp_path))
        assert result[0]["skill_installed"] is True

    def test_multiple_workspaces(self, tmp_path: Path):
        for name, marker in [("a", "CLAUDE.md"), ("b", ".cursorrules")]:
            ws = tmp_path / name
            ws.mkdir()
            (ws / marker).write_text("")
        result = detect_agent_workspaces(search_dir=str(tmp_path))
        assert len(result) == 2
        types = {r["agent_type"] for r in result}
        assert types == {"claude_code", "cursor"}

    def test_nonexistent_search_dir(self, tmp_path: Path):
        result = detect_agent_workspaces(search_dir=str(tmp_path / "nonexistent"))
        assert result == []


# ===================================================================
# _marker_to_agent_type
# ===================================================================


class TestMarkerToAgentType:
    """Verify marker-to-agent-type mapping."""

    @pytest.mark.parametrize("marker,expected", [
        ("CLAUDE.md", "claude_code"),
        ("claude.yaml", "claude_desktop"),
        (".cursorrules", "cursor"),
        (".windsurfrules", "windsurf"),
        ("AGENTS.md", "generic"),
        (".github/copilot", "copilot"),
        ("unknown_marker", "unknown"),
    ])
    def test_mapping(self, marker: str, expected: str):
        assert _marker_to_agent_type(marker) == expected


# ===================================================================
# _check_skill_installed
# ===================================================================


class TestCheckSkillInstalled:
    """Verify skill installation detection."""

    def test_not_installed(self, tmp_path: Path):
        assert _check_skill_installed(tmp_path) is False

    def test_installed_at_root(self, tmp_path: Path):
        (tmp_path / "SKILL.md").write_text("# Skill")
        assert _check_skill_installed(tmp_path) is True

    def test_installed_in_dev(self, tmp_path: Path):
        dev = tmp_path / ".dev"
        dev.mkdir()
        (dev / "SKILL.md").write_text("# Skill")
        assert _check_skill_installed(tmp_path) is True

    def test_installed_in_kiln_dir(self, tmp_path: Path):
        kiln_dir = tmp_path / ".kiln"
        kiln_dir.mkdir()
        (kiln_dir / "SKILL.md").write_text("# Skill")
        assert _check_skill_installed(tmp_path) is True


# ===================================================================
# install_skill
# ===================================================================


class TestInstallSkill:
    """Verify skill installation into workspaces."""

    def test_installs_to_workspace_root(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        # Create a fake SKILL.md source
        source = tmp_path / "source_skill.md"
        source.write_text("# Kiln Skill")
        with patch(
            "kiln.skill_manifest.get_skill_definition_path",
            return_value=source,
        ):
            result = install_skill(str(ws))
        assert result["success"] is True
        assert (ws / "SKILL.md").is_file()
        assert (ws / "SKILL.md").read_text() == "# Kiln Skill"

    def test_installs_to_dev_dir_when_present(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / ".dev").mkdir()
        source = tmp_path / "source_skill.md"
        source.write_text("# Kiln Skill")
        with patch(
            "kiln.skill_manifest.get_skill_definition_path",
            return_value=source,
        ):
            result = install_skill(str(ws))
        assert result["success"] is True
        assert (ws / ".dev" / "SKILL.md").is_file()
        assert "installed_path" in result

    def test_refuses_overwrite_without_force(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "SKILL.md").write_text("# Existing")
        source = tmp_path / "source_skill.md"
        source.write_text("# New")
        with patch(
            "kiln.skill_manifest.get_skill_definition_path",
            return_value=source,
        ):
            result = install_skill(str(ws))
        assert result["success"] is False
        assert "already exists" in result["error"]
        assert "existing_path" in result
        # Original content preserved
        assert (ws / "SKILL.md").read_text() == "# Existing"

    def test_overwrites_with_force(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "SKILL.md").write_text("# Old")
        source = tmp_path / "source_skill.md"
        source.write_text("# New")
        with patch(
            "kiln.skill_manifest.get_skill_definition_path",
            return_value=source,
        ):
            result = install_skill(str(ws), force=True)
        assert result["success"] is True
        assert (ws / "SKILL.md").read_text() == "# New"

    def test_missing_workspace(self, tmp_path: Path):
        result = install_skill(str(tmp_path / "nonexistent"))
        assert result["success"] is False
        assert "Workspace not found" in result["error"]

    def test_missing_skill_source(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        with patch(
            "kiln.skill_manifest.get_skill_definition_path",
            side_effect=FileNotFoundError("SKILL.md not found"),
        ):
            result = install_skill(str(ws))
        assert result["success"] is False
        assert "SKILL.md not found" in result["error"]

    def test_result_includes_paths(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        source = tmp_path / "source_skill.md"
        source.write_text("# Kiln")
        with patch(
            "kiln.skill_manifest.get_skill_definition_path",
            return_value=source,
        ):
            result = install_skill(str(ws))
        assert "installed_path" in result
        assert "source_path" in result
        assert str(source) == result["source_path"]
