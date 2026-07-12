"""Version metadata tests."""

from __future__ import annotations

import json
import re
from pathlib import Path

import kiln


def test_version_is_not_stale_literal() -> None:
    """Version should not regress to the old hardcoded value."""
    assert kiln.__version__ != "0.1.0"
    assert kiln.__version__ not in {"", "unknown"}


def test_server_json_version_matches_package() -> None:
    """server.json (the MCP-registry manifest) must not drift from the
    package version.

    Guard for the drift where server.json sat at 1.1.8 while the package
    shipped 1.1.9: nothing cross-checked the manifest, so the release
    bump silently skipped it.  Both the top-level ``version`` and every
    ``packages[].version`` must equal the pyproject version.  Whoever
    bumps the release bumps server.json in the same commit, or this goes
    red.  Read both from the same tree (not the installed metadata) so a
    worktree/editable install can't mask a drift.
    """
    root = Path(__file__).resolve().parents[2]
    pyproject_text = (root / "kiln" / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"\s*$', pyproject_text)
    assert match, "could not find version in kiln/pyproject.toml"
    pkg_version = match.group(1)

    manifest = json.loads((root / "server.json").read_text(encoding="utf-8"))
    found = [("top-level", manifest.get("version"))]
    found += [
        (f"packages[{i}]", pkg.get("version"))
        for i, pkg in enumerate(manifest.get("packages", []))
    ]
    drift = [(where, value) for where, value in found if value != pkg_version]
    assert not drift, (
        f"server.json version drift vs pyproject {pkg_version!r}: {drift}. "
        "Bump server.json in lockstep with the package version."
    )


def test_mcpb_manifest_version_matches_package() -> None:
    """mcpb/manifest.json must not drift from the package version.

    Same drift class as server.json (see test above): the MCPB bundle's
    manifest declares its own ``version`` field, and nothing structural
    ties it to ``kiln3d``'s pyproject version.  This only catches the
    *manifest's* version — the bundle also needs an actual repack
    (``mcpb pack``, which regenerates ``uv.lock`` and re-pins the
    resolved ``kiln3d`` version) at every release; see mcpb/README.md.
    """
    root = Path(__file__).resolve().parents[2]
    pyproject_text = (root / "kiln" / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"\s*$', pyproject_text)
    assert match, "could not find version in kiln/pyproject.toml"
    pkg_version = match.group(1)

    manifest_path = root / "mcpb" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest.get("version") == pkg_version, (
        f"mcpb/manifest.json version {manifest.get('version')!r} drifted from "
        f"pyproject {pkg_version!r}. Bump manifest.json and repack the bundle "
        "(see mcpb/README.md) in the same commit."
    )
