"""The lock file is the SBOM's source of truth — keep it honest.

Every release publishes a Software Bill of Materials: the list of
third-party packages inside kiln3d, which a customer's security scanner
reads to check for known vulnerabilities.  ``.github/workflows/sbom.yml``
builds that list by scanning ``kiln/requirements-lock.txt``.

That makes the lock file load-bearing in a way it did not used to be.
If a dependency is added to ``pyproject.toml`` and the lock file is not
regenerated, the SBOM still looks healthy — it lists 50-odd packages and
passes the workflow's non-empty check — while silently omitting the new
dependency.  A scanner would then report a clean bill of health for code
it never examined, which is worse than shipping no SBOM at all.

The 2026-07-20 incident that motivated this file was the harsher form of
the same failure: the workflow scanned ``dist/`` (an unopened wheel, which
exposes no package metadata), so every published SBOM listed *zero*
dependencies.  The workflow now scans the lock file and fails on an empty
result; these tests cover the subtler case that check cannot see — a lock
file that is well-formed but out of date.

Regeneration command lives in the header of ``requirements-lock.txt``.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

_KILN_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _KILN_ROOT / "pyproject.toml"
_LOCKFILE = _KILN_ROOT / "requirements-lock.txt"


def _normalize(name: str) -> str:
    """Reduce a requirement string to its comparable package name.

    ``"paho-mqtt>=2.0"`` -> ``"paho-mqtt"``; ``"PyYAML==6.0.3"`` ->
    ``"pyyaml"``.  PEP 503 treats ``-`` and ``_`` as equivalent and names
    as case-insensitive, so both are folded.
    """
    bare = re.split(r"[<>=!~\[;\s]", name.strip(), maxsplit=1)[0]
    return bare.lower().replace("_", "-")


def _declared_runtime_deps() -> list[str]:
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    return data["project"].get("dependencies", [])


def _locked_packages() -> dict[str, str]:
    """Map normalized package name -> pinned version from the lock file."""
    locked: dict[str, str] = {}
    for line in _LOCKFILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "==" not in line:
            continue
        name, version = line.split("==", 1)
        locked[_normalize(name)] = version.strip()
    return locked


def test_lockfile_exists_and_is_populated() -> None:
    """The SBOM is generated from this file; an empty one yields an empty SBOM."""
    assert _LOCKFILE.is_file(), f"{_LOCKFILE} is missing — the SBOM has no source"
    locked = _locked_packages()
    assert len(locked) >= 10, (
        f"requirements-lock.txt pins only {len(locked)} package(s). That is far "
        "too few for kiln3d and would produce a misleading SBOM. Regenerate it "
        "using the command in the file's header comment."
    )


def test_every_declared_dependency_is_locked() -> None:
    """A dependency in pyproject.toml but not the lock file is invisible to the SBOM."""
    locked = _locked_packages()
    missing = [
        dep for dep in _declared_runtime_deps() if _normalize(dep) not in locked
    ]
    assert not missing, (
        "These runtime dependencies are declared in pyproject.toml but absent "
        f"from requirements-lock.txt: {missing}.\n\n"
        "The published SBOM is generated from the lock file, so it would ship "
        "without them — a customer's vulnerability scanner would never inspect "
        "that code. Regenerate the lock file using the command in its header "
        "comment, then commit it alongside the dependency change."
    )


def test_lockfile_pins_exact_versions() -> None:
    """Ranges rather than pins would make the SBOM's versions untrustworthy."""
    unpinned = []
    for line in _LOCKFILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-e"):
            continue
        if "==" not in line:
            unpinned.append(line)
    assert not unpinned, (
        f"requirements-lock.txt has entries that are not pinned with '==': "
        f"{unpinned}. The SBOM reports the version of every component, so each "
        "entry must name one exact version."
    )


def _sbom_workflow_body() -> str:
    workflow = _KILN_ROOT.parent / ".github" / "workflows" / "sbom.yml"
    if not workflow.is_file():
        pytest.skip("sbom.yml not present in this checkout")
    return workflow.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "expected",
    [
        # The lock is applied as CONSTRAINTS, not as the scan target:
        # constraints keep the tested pins while installing only what the
        # product actually requires at runtime.
        "-c kiln/requirements-lock.txt",
        # The scan target is the resolved runtime set.
        "syft file:runtime-requirements.txt",
        # The non-empty check that stops a hollow SBOM being published.
        "Verify the SBOMs actually list dependencies",
    ],
)
def test_sbom_workflow_generates_from_the_runtime_closure(expected: str) -> None:
    """Pin the properties of sbom.yml that this file's guarantees rest on."""
    assert expected in _sbom_workflow_body(), (
        f"sbom.yml no longer contains {expected!r}. The published SBOM must "
        "describe the runtime dependency closure, pinned to the locked "
        "versions — see the module docstring for the two ways this has gone "
        "wrong before."
    )


def test_sbom_workflow_does_not_scan_the_build_output() -> None:
    """Scanning dist/ is the original zero-component bug; keep it out."""
    body = _sbom_workflow_body()
    assert "syft dir:dist" not in body, (
        "sbom.yml scans dist/ again. syft catalogues installed packages by "
        "reading .dist-info directories, and a built wheel is an unopened "
        "archive — this silently republishes an SBOM listing zero "
        "dependencies."
    )


def test_sbom_workflow_does_not_scan_the_dev_lock_directly() -> None:
    """Scanning the full lock republishes test tooling as shipped code."""
    body = _sbom_workflow_body()
    assert "syft file:kiln/requirements-lock.txt" not in body, (
        "sbom.yml scans requirements-lock.txt directly again. That file is a "
        "full dev freeze, so the published SBOM would once more list pytest, "
        "coverage and other test-only packages as though they ship inside "
        "kiln3d — giving reviewers vulnerability hits on code no user runs."
    )
