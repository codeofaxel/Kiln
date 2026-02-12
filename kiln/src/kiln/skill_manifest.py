"""Skill manifest generation and distribution for Kiln.

Provides the skill definition, configuration requirements, and tool
catalog so that AI agents can self-discover Kiln's capabilities without
manual SKILL.md file copying.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class SkillManifest:
    """Machine-readable skill manifest for agent integration."""

    name: str = "kiln"
    version: str = ""  # populated from package version
    description: str = "3D printer control and monitoring via CLI and MCP"

    # Configuration requirements
    required_env: List[str] = field(default_factory=lambda: [
        "KILN_PRINTER_HOST",
        "KILN_PRINTER_API_KEY",
        "KILN_PRINTER_TYPE",
    ])
    optional_env: List[str] = field(default_factory=lambda: [
        "KILN_PRINTER_MODEL",
        "KILN_PRINTER_SERIAL",
        "KILN_AUTONOMY_LEVEL",
        "KILN_HEATER_TIMEOUT",
        "KILN_CRAFTCLOUD_API_KEY",
        "KILN_SCULPTEO_API_KEY",
    ])

    # Capabilities
    interfaces: List[str] = field(default_factory=lambda: ["cli", "mcp"])
    tool_count: int = 0  # populated dynamically
    safety_levels: List[str] = field(default_factory=lambda: [
        "safe", "guarded", "confirm", "emergency",
    ])

    # Setup verification
    setup_command: str = "kiln verify"
    health_command: str = "kiln status --json"

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict (enum-safe)."""
        return asdict(self)


def get_version() -> str:
    """Get the installed Kiln package version."""
    try:
        from importlib.metadata import version
        return version("kiln")
    except Exception:
        return "unknown"


def get_tool_count() -> int:
    """Count available MCP tools from the safety classification file."""
    try:
        data_path = Path(__file__).resolve().parent / "data" / "tool_safety.json"
        if data_path.is_file():
            raw = json.loads(data_path.read_text(encoding="utf-8"))
            return len(raw.get("classifications", {}))
    except (json.JSONDecodeError, OSError, KeyError):
        pass
    return 0


def generate_manifest() -> SkillManifest:
    """Generate a complete skill manifest."""
    return SkillManifest(
        version=get_version(),
        tool_count=get_tool_count(),
    )


def get_skill_definition_path() -> Path:
    """Return the path to the bundled SKILL.md file.

    Searches common locations relative to the package and the user home
    directory.  Raises :class:`FileNotFoundError` if no SKILL.md is found.
    """
    candidates = [
        Path(__file__).resolve().parent.parent.parent.parent / "SKILL.md",
        Path(__file__).resolve().parent / "data" / "SKILL.md",
        Path.home() / ".kiln" / "SKILL.md",
        # Repo .dev/ location
        Path(__file__).resolve().parent.parent.parent.parent / ".dev" / "SKILL.md",
    ]
    for p in candidates:
        if p.is_file():
            return p
    raise FileNotFoundError(
        "SKILL.md not found. Run 'pip install kiln' or check your installation."
    )


_AGENT_WORKSPACE_MARKERS: List[str] = [
    "CLAUDE.md",         # Claude Code
    "claude.yaml",       # Claude Desktop
    ".cursorrules",      # Cursor
    ".windsurfrules",    # Windsurf
    "AGENTS.md",         # Generic agent workspace
    ".github/copilot",   # GitHub Copilot
]


def detect_agent_workspaces(*, search_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """Detect AI agent workspaces in common locations.

    Searches for marker files that indicate an agent workspace.
    Returns a list of dicts with path, agent_type, and marker info.
    """
    results: List[Dict[str, Any]] = []

    search_paths: List[Path] = []
    if search_dir:
        search_paths.append(Path(search_dir))
    else:
        home = Path.home()
        search_paths.extend([
            Path.cwd(),
            home / "Documents",
            home / "Projects",
            home / "Code",
            home / "Developer",
            home / "dev",
        ])

    seen: set[str] = set()
    for base in search_paths:
        if not base.is_dir():
            continue
        _scan_dir_for_markers(base, seen, results)
        # One level of subdirectories
        try:
            for entry in base.iterdir():
                if entry.is_dir():
                    _scan_dir_for_markers(entry, seen, results)
        except PermissionError:
            continue

    return results


def _scan_dir_for_markers(
    directory: Path,
    seen: set[str],
    results: List[Dict[str, Any]],
) -> None:
    """Check *directory* for agent workspace marker files."""
    resolved = str(directory.resolve())
    if resolved in seen:
        return
    seen.add(resolved)

    for marker in _AGENT_WORKSPACE_MARKERS:
        marker_path = directory / marker
        if marker_path.exists():
            results.append({
                "path": str(directory),
                "agent_type": _marker_to_agent_type(marker),
                "marker": marker,
                "skill_installed": _check_skill_installed(directory),
            })
            break


def _marker_to_agent_type(marker: str) -> str:
    """Map a marker filename to an agent type name."""
    mapping: Dict[str, str] = {
        "CLAUDE.md": "claude_code",
        "claude.yaml": "claude_desktop",
        ".cursorrules": "cursor",
        ".windsurfrules": "windsurf",
        "AGENTS.md": "generic",
        ".github/copilot": "copilot",
    }
    return mapping.get(marker, "unknown")


def _check_skill_installed(workspace: Path) -> bool:
    """Check if the Kiln skill is already installed in a workspace."""
    skill_locations = [
        workspace / ".dev" / "SKILL.md",
        workspace / "SKILL.md",
        workspace / ".kiln" / "SKILL.md",
    ]
    return any(p.is_file() for p in skill_locations)


def install_skill(workspace_path: str, *, force: bool = False) -> Dict[str, Any]:
    """Install the Kiln skill definition into an agent workspace.

    Copies SKILL.md to the appropriate location based on workspace layout.
    Returns a dict with installation result info.
    """
    workspace = Path(workspace_path)
    if not workspace.is_dir():
        return {"success": False, "error": f"Workspace not found: {workspace_path}"}

    try:
        source = get_skill_definition_path()
    except FileNotFoundError as exc:
        return {"success": False, "error": str(exc)}

    # Prefer .dev/ if it exists, otherwise workspace root
    dev_dir = workspace / ".dev"
    if dev_dir.is_dir():
        target = dev_dir / "SKILL.md"
    else:
        target = workspace / "SKILL.md"

    if target.is_file() and not force:
        return {
            "success": False,
            "error": f"SKILL.md already exists at {target}. Use --force to overwrite.",
            "existing_path": str(target),
        }

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(source), str(target))

    return {
        "success": True,
        "installed_path": str(target),
        "source_path": str(source),
    }
