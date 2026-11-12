"""A pull moves a branch here forward, and never off versions only this computer has.

The branch's head here follows the account's when the account's head grows
from it.  When this computer is ahead, or the two went different ways, the
head here stays where it is, the pulled versions are still kept, and the
answer says so.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_design_tie import _Accounts, _folder, _recipe  # noqa: E402

from kiln import history_courier as courier  # noqa: E402
from kiln.history_vault import HistoryVault  # noqa: E402

IDS = [f"{n:012x}" + "a" * 20 for n in (1, 2, 3)]
OTHER = "f" * 12 + "b" * 20


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("KILN_DESIGNS_DIR", raising=False)
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    return tmp_path


def _here(home, count: int) -> None:
    for number, vid in enumerate(IDS[:count], start=1):
        _recipe(_folder(home), number, vid)
    vault = HistoryVault()
    try:
        vault.sync_from_library("vase")
    finally:
        vault.close()


def _account(monkeypatch, versions: list[str]) -> None:
    accounts = _Accounts()
    accounts.designs["X"]["versions"] = [(vid, n) for n, vid in enumerate(versions, start=1)]
    monkeypatch.setattr(courier, "_request", accounts.request)


def _main() -> tuple[str, list[str]]:
    vault = HistoryVault()
    try:
        branch = vault.branch_named("vase", "main")
        return branch["head_version_id"], [m["version_id"] for m in vault.memberships(branch["branch_id"])]
    finally:
        vault.close()


def test_a_pull_leaves_the_head_on_a_version_only_this_computer_has(home, monkeypatch):
    _here(home, 3)
    _account(monkeypatch, IDS[:2])

    pulled = courier.pull("cloud_pull_branch", "vase")

    assert pulled["status"] == "success", pulled
    head, members = _main()
    assert head == IDS[2] and members == IDS
    assert "main" in pulled["applied"]["heads_kept_here"]
    assert "has versions your Kiln account does not" in pulled["said_heads"]


def test_a_pull_moves_the_head_forward_when_the_account_is_ahead(home, monkeypatch):
    _here(home, 2)
    _account(monkeypatch, IDS)

    pulled = courier.pull("cloud_pull_branch", "vase")

    assert pulled["status"] == "success", pulled
    head, members = _main()
    assert head == IDS[2] and members == IDS
    assert "heads_kept_here" not in pulled["applied"]


def test_a_pull_of_a_branch_that_went_another_way_leaves_it_and_says_to_push_first(home, monkeypatch):
    _here(home, 3)
    _account(monkeypatch, [IDS[0], IDS[1], OTHER])

    pulled = courier.pull("cloud_pull_branch", "vase")

    assert pulled["status"] == "success", pulled
    head, members = _main()
    assert head == IDS[2] and members == IDS
    assert pulled["applied"]["heads_kept_here"]["main"] == "went another way"
    assert "nothing was merged" in pulled["said_heads"] and "cloud push" in pulled["said_heads"]


def test_once_this_computers_line_is_on_a_branch_of_its_own_the_pull_follows_the_account(home, monkeypatch):
    """After a push kept this computer's work as ``main@<computer>``, a pull
    moves ``main`` to the account's line and takes the kept versions off
    ``main``: they are whole on their own branch."""
    _here(home, 3)
    vault = HistoryVault()
    try:
        main = vault.branch_named("vase", "main")
        vault.keep_branch_copy(
            {**main, "branch_id": "kept-copy", "name": "main@this-computer", "parent_branch_id": main["branch_id"]},
            [{"version_id": v} for v in IDS],
        )
    finally:
        vault.close()
    _account(monkeypatch, [IDS[0], IDS[1], OTHER])

    pulled = courier.pull("cloud_pull_branch", "vase")

    assert pulled["status"] == "success", pulled
    head, members = _main()
    assert head == OTHER and members == [IDS[0], IDS[1], OTHER]
    assert "heads_kept_here" not in pulled["applied"]
    vault = HistoryVault()
    try:
        assert [m["version_id"] for m in vault.memberships("kept-copy")] == IDS
    finally:
        vault.close()
