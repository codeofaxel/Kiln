"""Workflows that can publish run only code pinned to an exact version.

A job that can publish (an OIDC token, write access to the repository, or a
repository secret) shares its workflow with every other job in it: build jobs
hand it the artifacts it uploads.  So in any workflow holding such a job:

* every ``uses:`` names a full commit SHA, because a tag or branch can be moved
  to different code after it was reviewed;
* nothing fetched from a moving address (a ``latest`` release, a branch) is
  piped into a shell or unpacked and run, unless the address is a commit;
* every npm tool installed globally or run with ``npx`` names an exact version;
* the OIDC grant sits on the jobs that use it, not on a workflow of many jobs.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml")

_WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"

_PINNED = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
_COMMIT_IN_URL = re.compile(r"/[0-9a-f]{40}/")
_DOWNLOAD = re.compile(r"\b(curl|wget)\b")
_PIPED_TO_RUN = re.compile(r"\|\s*(?:sudo\s+)?(?:sh|bash|tar)\b")
_EXACT_VERSION = re.compile(r"^@\d+\.\d+\.\d+$")
_NPM_TOOL = re.compile(
    r"\b(?:npm\s+(?:install|i)\s+(?:-g|--global)|npx(?:\s+(?:--yes|-y))?)\s+"
    r"((?:@[\w.-]+/)?[\w.-]+)(@\S+)?"
)
_REPO_SECRET = re.compile(r"secrets\.(?!GITHUB_TOKEN\b)\w+")


def _grants(block: Any) -> dict[str, str]:
    if block == "write-all":
        return {"id-token": "write", "contents": "write"}
    if isinstance(block, dict):
        return {str(k): str(v) for k, v in block.items()}
    return {}


def _can_publish(job: dict[str, Any], workflow_grants: dict[str, str]) -> bool:
    grants = _grants(job["permissions"]) if "permissions" in job else workflow_grants
    if grants.get("id-token") == "write" or grants.get("contents") == "write":
        return True
    return bool(_REPO_SECRET.search(str(job)))


def violations(name: str, workflow: dict[str, Any]) -> list[str]:
    """Every rule this module states, broken in one parsed workflow."""
    jobs = workflow.get("jobs") or {}
    workflow_grants = _grants(workflow.get("permissions"))
    found: list[str] = []

    if workflow_grants.get("id-token") == "write" and len(jobs) > 1:
        found.append(f"{name}: grants id-token: write to all {len(jobs)} jobs; grant it per job")

    if not any(_can_publish(job, workflow_grants) for job in jobs.values()):
        return found

    for job_name, job in jobs.items():
        where = f"{name}:{job_name}"
        reusable = job.get("uses")
        if reusable and not str(reusable).startswith("./") and not _PINNED.match(str(reusable)):
            found.append(f"{where}: reusable workflow {reusable} is not pinned to a commit")
        for step in job.get("steps") or []:
            label = f"{where}:{step.get('name') or step.get('uses') or 'run'}"
            action = step.get("uses")
            if action and not str(action).startswith("./") and not _PINNED.match(str(action)):
                found.append(f"{label}: {action} is not pinned to a commit")
            script = str(step.get("run") or "").replace("\\\n", " ")
            for line in script.splitlines():
                if _DOWNLOAD.search(line) and "releases/latest" in line:
                    found.append(f"{label}: downloads a 'latest' release")
                elif (
                    _DOWNLOAD.search(line)
                    and _PIPED_TO_RUN.search(line)
                    and not _COMMIT_IN_URL.search(line)
                ):
                    found.append(f"{label}: pipes a download from a moving address into a shell or archive")
                for tool, version in _NPM_TOOL.findall(line):
                    if not _EXACT_VERSION.match(version or ""):
                        found.append(f"{label}: npm tool {tool} has no exact version")
    return found


def _real_workflows() -> list[Path]:
    if not _WORKFLOWS.is_dir():
        pytest.skip("no .github/workflows beside this checkout")
    return sorted(_WORKFLOWS.glob("*.yml")) + sorted(_WORKFLOWS.glob("*.yaml"))


def test_publishing_workflows_run_only_pinned_code() -> None:
    found: list[str] = []
    for path in _real_workflows():
        found += violations(path.name, yaml.safe_load(path.read_text()))
    assert not found, "unpinned code in a workflow that can publish:\n" + "\n".join(found)


_BAD = """
permissions:
  id-token: write
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - run: curl -L https://example.invalid/releases/latest/download/tool.tar.gz | tar xz tool
      - run: |
          curl -sSfL https://example.invalid/main/install.sh \\
            | sh -s -- -b /usr/local/bin
      - run: npm install -g sometool
  publish:
    runs-on: ubuntu-latest
    steps:
      - uses: pypa/gh-action-pypi-publish@release/v1
"""

_GOOD = """
permissions:
  contents: read
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1
      - run: |
          curl -sSfL https://example.invalid/5e16e5031a13f8a11057feb8544decebfc43b4ed/install.sh \\
            | sh -s -- -b /usr/local/bin
      - run: npm install -g sometool@1.2.3 && npx --yes @scope/pack@2.1.2 pack .
  publish:
    runs-on: ubuntu-latest
    permissions:
      id-token: write
    steps:
      - uses: pypa/gh-action-pypi-publish@7245906fb61fa33a13554f9d5fb856fe61758086  # v1.14.3
"""


def test_the_check_catches_each_shape() -> None:
    found = "\n".join(violations("bad.yml", yaml.safe_load(_BAD)))
    for shape in (
        "grants id-token: write to all 2 jobs",
        "actions/checkout@v7 is not pinned",
        "downloads a 'latest' release",
        "pipes a download from a moving address",
        "npm tool sometool has no exact version",
        "pypa/gh-action-pypi-publish@release/v1 is not pinned",
    ):
        assert shape in found, f"missed: {shape}\n{found}"


def test_the_check_passes_pinned_shapes() -> None:
    assert violations("good.yml", yaml.safe_load(_GOOD)) == []
