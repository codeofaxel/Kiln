"""Tests for skill manifest generation and distribution.

Covers:
    - SkillManifest dataclass defaults and to_dict serialization
    - the Pro tier line names no free capability as Kiln Pro's and reads each
      plan fact from the code that owns it
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

import json
import re
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
        assert m.mcp_capability_count == 0
        assert "safe" in m.safety_levels
        assert m.setup_command == "kiln verify"
        assert m.health_command == "kiln status --json"

    def test_to_dict_returns_dict(self):
        m = SkillManifest(version="1.0.0", tool_count=42, mcp_capability_count=49)
        d = m.to_dict()
        assert isinstance(d, dict)
        assert d["name"] == "kiln"
        assert d["version"] == "1.0.0"
        assert d["tool_count"] == 42
        assert d["mcp_capability_count"] == 49

    def test_to_dict_all_keys_present(self):
        d = SkillManifest().to_dict()
        expected_keys = {
            "name", "version", "description",
            "required_env", "optional_env",
            "interfaces", "tool_count", "mcp_capability_count", "safety_levels",
            "setup_command", "health_command",
            "discovery", "tiers",
            "agent_rules", "tool_recommendations", "workflows",
        }
        assert set(d.keys()) == expected_keys

    def test_to_dict_lists_are_plain_lists(self):
        d = SkillManifest().to_dict()
        assert isinstance(d["required_env"], list)
        assert isinstance(d["optional_env"], list)
        assert isinstance(d["interfaces"], list)
        assert isinstance(d["safety_levels"], list)

    def test_workflow_takes_a_flat_part_from_relationships_to_a_preview(self):
        steps = SkillManifest().to_dict()["workflows"]["flat_part_from_relationships"]
        order = [next(tool for tool in ("solve_sketch", "compile_scad", "visualize_model")
                      if tool in step) for step in steps[:3]]
        assert order == ["solve_sketch", "compile_scad", "visualize_model"]
        assert "unchanged" in steps[1]


# ===================================================================
# The Pro tier line
# ===================================================================

#: The served catalogue the line reads: written by kiln-pro, bundled here.
_SERVED = Path(__file__).resolve().parents[1] / "src" / "kiln" / "pro_tool_manifest.json"

#: How a sentence names each family of served tools, by catalogue category.
#: Only the words: which plan gets what is read from the catalogue.
_FAMILY_WORDS: dict[str, tuple[str, ...]] = {
    "product_generators": ("product generator", "generators"),
    "design_versioning": ("version control", "versioning", "design history", "branch"),
    "cloud_sync": ("cloud sync", "sync"),
    "textures": ("texture",),
    "decoration": ("decoration",),
}

#: The line this replaced, which named five families a free caller reaches as
#: Kiln Pro's.
_REPLACED_LINE = (
    "Product generators (coasters, keychains, ornaments, etc.), decoration, "
    "procedural textures, cloud sync, design versioning, manual speed control, "
    "print learning. Still 1 printer — fleet starts at Business. $49/mo. "
    "https://kiln3d.com/pricing"
)


def _catalogue() -> dict:
    return json.loads(_SERVED.read_text(encoding="utf-8"))


def _free_claimed_as_pro(line: str, catalogue: dict) -> list[str]:
    """Each sentence of *line* that names a family a free caller reaches as Kiln Pro's.

    A family whose every tool is free may not be named at all.  One with a
    monthly allowance may be named only as unlimited; one with a band, only
    with the band's own words for Pro.
    """
    sentences = [s.lower() for s in re.split(r"(?<=[.;])\s+", line) if s.strip()]
    found = []
    for family, words in _FAMILY_WORDS.items():
        members = [t for t in catalogue["tools"] if t.get("category") == family]
        assert members, f"the catalogue has no {family} tools, so the check would see nothing"
        access = {t.get("access") for t in members}
        bands = {
            t["band"]["pro"].lower()
            for t in members
            if isinstance(t.get("band"), dict) and t["band"].get("pro")
        }
        for sentence in sentences:
            if not any(word in sentence for word in words):
                continue
            if access <= {"free"}:
                found.append(f"{family} is free at every door: {sentence!r}")
            elif "free_metered" in access and "unlimited" not in sentence:
                found.append(f"{family} is free within a monthly allowance: {sentence!r}")
            elif "free_banded" in access and not any(band in sentence for band in bands):
                found.append(f"{family} is free within its band: {sentence!r}")
    return found


class TestProTierLine:
    """The Pro line names no free capability as Kiln Pro's, and reads its
    summary, history band, printer cap and link from the code that owns them."""

    def test_names_no_free_capability_as_pro(self):
        line = SkillManifest().tiers["pro"]
        assert _free_claimed_as_pro(line, _catalogue()) == []

    def test_the_check_refuses_the_line_it_replaced(self):
        found = " ".join(_free_claimed_as_pro(_REPLACED_LINE, _catalogue()))
        for family in _FAMILY_WORDS:
            assert family in found, f"the check let the old line name {family} as Pro"

    def test_each_fact_is_read_from_its_owner(self):
        from kiln.licensing import max_printers_for_tier
        from kiln.tiers_and_terms import PRICING_URL

        catalogue = _catalogue()
        line = SkillManifest().tiers["pro"]
        assert catalogue["tiers"]["pro"]["benefits"] in line
        band = next(
            t["band"] for t in catalogue["tools"]
            if (t.get("band") or {}).get("domain") == "design_versions"
        )
        assert band["pro"] in line
        cap = max_printers_for_tier("pro")
        assert ("One printer at a time" if cap == 1 else f"Up to {cap} printers") in line
        assert PRICING_URL in line
        assert "$" not in line, "a price typed into the line; the pricing page holds it"

    def test_a_changed_cap_or_summary_changes_the_line(self, monkeypatch):
        from kiln import skill_manifest

        monkeypatch.setattr(
            "kiln.licensing.max_printers_for_tier",
            lambda tier: 2 if tier == "pro" else 50,
        )
        monkeypatch.setattr(
            skill_manifest,
            "_served_catalogue",
            lambda: {"tiers": {"pro": {"benefits": "Everything in Free, plus a test."}}, "tools": []},
        )
        line = skill_manifest.pro_tier_line()
        assert line.startswith("Everything in Free, plus a test. Up to 2 printers at once.")

    def test_without_a_catalogue_it_names_no_capability(self, monkeypatch):
        from kiln import skill_manifest
        from kiln.tiers_and_terms import PRICING_URL

        monkeypatch.setattr(skill_manifest, "_served_catalogue", lambda: {})
        line = skill_manifest.pro_tier_line()
        assert line == f"{skill_manifest._pro_printer_clause()} {PRICING_URL}"


# ===================================================================
# Version and tool count helpers
# ===================================================================


class TestGetVersion:
    """Verify get_version returns a string."""

    def test_returns_string(self):
        v = get_version()
        assert isinstance(v, str)
        assert len(v) > 0

    def test_survives_unusable_package_metadata(self):
        """Broken metadata costs the version nothing — the source tree has it.

        This used to assert ``"unknown"``, which described the defect
        rather than the contract: the lookup asked for ``"kiln"`` while
        the distribution is ``kiln3d``, so it raised on every ordinary
        install and the bare ``except`` published ``"unknown"`` as the
        manifest's version field.  A test that pins the fallback string
        cannot tell that apart from the fallback firing constantly.

        ``get_version`` now delegates to :data:`kiln.__version__`, which
        reads the source tree before it asks metadata anything, so the
        honest expectation is a real version even here.
        """
        import kiln

        with patch(
            "importlib.metadata.version",
            side_effect=ImportError("no package"),
        ):
            result = get_version()

        assert result == kiln.__version__
        assert result != "unknown"


class TestGetToolCount:
    """Verify get_tool_count queries the live MCP registry with fallback."""

    def test_returns_positive_int(self):
        count = get_tool_count()
        assert isinstance(count, int)
        assert count > 0  # live MCP registry or classification fallback

    def test_returns_zero_when_both_sources_fail(self, tmp_path: Path):
        """When live registry AND classification file both fail, return 0."""
        bad_json = tmp_path / "data" / "tool_safety.json"
        bad_json.parent.mkdir(parents=True)
        bad_json.write_text("not valid json")
        import kiln.skill_manifest as mod
        original_file = mod.__file__
        try:
            # Point __file__ so the classification fallback finds bad JSON
            mod.__file__ = str(tmp_path / "skill_manifest.py")
            # Mock the live registry so it also fails
            with patch("kiln.server.mcp") as mock_mcp:
                mock_mcp._tool_manager.list_tools.side_effect = RuntimeError(
                    "registry unavailable"
                )
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
        assert m.mcp_capability_count >= m.tool_count

    def test_returns_skill_manifest_instance(self):
        m = generate_manifest()
        assert isinstance(m, SkillManifest)

    def test_to_dict_roundtrip(self):
        m = generate_manifest()
        d = m.to_dict()
        assert d["name"] == "kiln"
        assert isinstance(d["tool_count"], int)
        assert isinstance(d["mcp_capability_count"], int)


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
