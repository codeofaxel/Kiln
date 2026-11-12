"""A branch is kept on this computer under the id the account keeps it under.

A push says this Kiln takes the account's id for a branch; when the answer
names another id than the one sent, the branch here is that id from then on,
with its versions, its place as the checked-out branch and its log.  A pull
or clone that brings a branch whose name a branch here already has, under
another id, is one branch afterwards.  An id another design here holds is
never taken.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_design_tie import KEY_X, LINE_X, _Accounts, _folder, _recipe  # noqa: E402

from kiln import history_courier as courier  # noqa: E402
from kiln.history_vault import HistoryVault, _main_branch_id  # noqa: E402

ACCOUNT_ID = "acc0" * 8
IDS = [f"{n:012x}" + "a" * 20 for n in (1, 2, 3)]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("KILN_DESIGNS_DIR", raising=False)
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    return tmp_path


class _Account:
    """An account that keeps every pushed branch under an id of its own."""

    def __init__(self) -> None:
        self.pushes: list[dict] = []
        self.ids: dict[str, str] = {}
        self.diverged_once = False

    def request(self, tool, method, path, body=None, **_kw):
        if path.endswith("/branches/push"):
            self.pushes.append(body)
            name = body["branch"]["name"]
            if self.diverged_once and "@" not in name:
                self.diverged_once = False
                return {"status": "error", "http_status": 409, "code": "BRANCH_DIVERGED",
                        "error": "diverged", "remote_head": "f" * 32, "local_parent": None}
            kept = self.ids.setdefault(name, ACCOUNT_ID if name == "main" else "0wn0" * 8)
            ids = [v["version_id"] for v in body["versions"]]
            numbers = {vid: i + 1 for i, vid in enumerate(ids)}
            return {"pushed_versions": ids, "skipped_versions": [], "branch_id": kept,
                    "version_numbers": numbers, "account_numbers": numbers,
                    "version_number_mark": len(ids), "design_key": KEY_X, "design_line": LINE_X}
        if path.endswith("/proven"):
            return {"recipes": []}
        if path.endswith("/meshes/stored"):
            return {"stored": []}
        return {"status": "error", "code": "NOT_EMULATED", "error": path}


@pytest.fixture
def account(home, monkeypatch):
    stand_in = _Account()
    monkeypatch.setattr(courier, "_request", stand_in.request)
    return stand_in


def _made_here(home) -> str:
    """A design with three versions made on this computer; its main branch's id here."""
    for number, vid in enumerate(IDS, start=1):
        _recipe(_folder(home), number, vid)
    vault = HistoryVault()
    try:
        vault.sync_from_library("vase")
        return vault.branch_named("vase", "main")["branch_id"]
    finally:
        vault.close()


def _mains() -> list[dict]:
    vault = HistoryVault()
    try:
        return [b for b in vault.branches("vase") if b["name"] == "main" and b.get("status") != "retired"]
    finally:
        vault.close()


class TestAPushTakesTheAccountsId:
    def test_a_push_says_this_kiln_takes_the_accounts_id(self, home, account):
        _made_here(home)

        assert courier.push("cloud_push_branch", "vase")["status"] == "success"

        assert account.pushes[0]["branch_ids"] == "account"

    def test_the_branch_here_is_the_accounts_id_after_the_push(self, home, account):
        made_as = _made_here(home)
        assert made_as != ACCOUNT_ID

        out = courier.push("cloud_push_branch", "vase")

        assert out["branch_id"] == ACCOUNT_ID
        mains = _mains()
        assert [b["branch_id"] for b in mains] == [ACCOUNT_ID]
        vault = HistoryVault()
        try:
            assert vault.branch(made_as) is None
            assert [m["version_id"] for m in vault.memberships(ACCOUNT_ID)] == IDS
            assert vault.memberships(made_as) == []
            assert vault.checked_out("vase")["branch_id"] == ACCOUNT_ID
        finally:
            vault.close()

    def test_the_next_push_names_the_branch_by_the_accounts_id(self, home, account):
        _made_here(home)
        courier.push("cloud_push_branch", "vase")

        courier.push("cloud_push_branch", "vase")

        assert account.pushes[1]["branch"]["branch_id"] == ACCOUNT_ID
        assert {m["branch_id"] for m in account.pushes[1]["memberships"]} == {ACCOUNT_ID}

    def test_a_copy_kept_as_this_computers_takes_the_accounts_id_too(self, home, account):
        _made_here(home)
        account.diverged_once = True

        out = courier.push("cloud_push_branch", "vase")

        assert out["status"] == "success" and out["kept_as"].startswith("main@")
        assert out["branch_id"] == "0wn0" * 8
        vault = HistoryVault()
        try:
            kept = vault.branch_named("vase", out["kept_as"])
            assert kept["branch_id"] == "0wn0" * 8
            assert [m["version_id"] for m in vault.memberships("0wn0" * 8)] == IDS
        finally:
            vault.close()


class TestAPullBringsOneBranchOfAName:
    def test_a_pull_of_a_branch_this_computer_has_under_another_id_leaves_one(self, home, monkeypatch):
        made_as = _made_here(home)
        accounts = _Accounts()
        accounts.designs["X"]["versions"] = [(vid, n) for n, vid in enumerate(IDS, start=1)]
        monkeypatch.setattr(courier, "_request", accounts.request)

        pulled = courier.pull("cloud_pull_branch", "vase")

        assert pulled["status"] == "success", pulled
        assert [b["branch_id"] for b in _mains()] == ["X-main"]
        vault = HistoryVault()
        try:
            assert vault.branch(made_as) is None
            assert [m["version_id"] for m in vault.memberships("X-main")] == IDS
            assert vault.checked_out("vase")["branch_id"] == "X-main"
        finally:
            vault.close()


class TestTheVaultsOwnRule:
    def test_everything_recorded_under_the_old_id_follows(self, home):
        made_as = _made_here(home)
        vault = HistoryVault()
        try:
            vault.keep_branch_copy(
                {"branch_id": "side", "design_id": "vase", "name": "side", "parent_branch_id": made_as,
                 "base_version_id": IDS[0], "head_version_id": IDS[0], "created_at": 1.0, "status": "active"},
                [{"version_id": IDS[0]}],
            )
            vault.check_out("vase", made_as)

            assert vault.take_account_branch_id("vase", made_as, ACCOUNT_ID) is True

            assert vault.branch(made_as) is None
            assert vault.branch(ACCOUNT_ID)["name"] == "main"
            assert vault.branch("side")["parent_branch_id"] == ACCOUNT_ID
            assert vault.checked_out("vase")["branch_id"] == ACCOUNT_ID
            assert vault.take_account_branch_id("vase", made_as, ACCOUNT_ID) is False
        finally:
            vault.close()

    def test_an_id_another_design_here_holds_is_never_taken(self, home):
        made_as = _made_here(home)
        vault = HistoryVault()
        try:
            other = _main_branch_id("bowl")
            vault.keep_branch_copy(
                {"branch_id": other, "design_id": "bowl", "name": "main", "created_at": 1.0, "status": "active"}, [],
            )

            assert vault.take_account_branch_id("vase", made_as, other) is False

            assert vault.branch(made_as)["design_id"] == "vase"
            assert vault.branch(other)["design_id"] == "bowl"
            assert [m["version_id"] for m in vault.memberships(made_as)] == IDS
        finally:
            vault.close()


class TestTheLapsCases:
    def test_two_ids_already_here_become_one_branch_with_each_version_in_one_place(self, home):
        made_as = _made_here(home)
        vault = HistoryVault()
        try:
            vault.keep_branch_copy(
                {"branch_id": ACCOUNT_ID, "design_id": "vase", "name": "main", "created_at": 1.0, "status": "retired"},
                [{"version_id": IDS[0]}, {"version_id": "f" * 32}],
            )

            assert vault.take_account_branch_id("vase", made_as, ACCOUNT_ID) is True

            places = [m["position"] for m in vault.memberships(ACCOUNT_ID)]
            assert places == list(range(len(places))) and len(places) == 4
        finally:
            vault.close()

    def test_a_pull_by_the_id_this_computer_made_for_main_asks_by_name(self, home, monkeypatch):
        asked: list[dict] = []

        def request(tool, method, path, body=None, **_kw):
            asked.append(body or {})
            return {"status": "error", "code": "STOP", "error": "enough"}

        monkeypatch.setattr(courier, "_request", request)

        courier.pull("cloud_pull_branch", "vase", branch_id=_main_branch_id("vase"))

        assert asked[0]["branch_id"] is None and asked[0]["branch_name"] == "main"

    def test_the_automatic_push_reports_the_id_the_branch_is_kept_under(self, home, account):
        made_as = _made_here(home)
        vault = HistoryVault()
        try:
            rows = courier._auto_push({"vase": [made_as]}, vault)
        finally:
            vault.close()

        assert ACCOUNT_ID in str(rows) and made_as not in str(rows)

    def test_a_second_diverged_push_finds_this_computers_copy_by_its_name(self, home, account):
        _made_here(home)
        account.diverged_once = True
        first = courier.push("cloud_push_branch", "vase")
        vault = HistoryVault()
        try:
            main = vault.branch_named("vase", "main")["branch_id"]
            made_at = vault.branch_named("vase", first["kept_as"])["created_at"]
        finally:
            vault.close()
        account.diverged_once = True

        second = courier.push("cloud_push_branch", "vase", main)

        assert second["kept_as"] == first["kept_as"] and second["branch_id"] == first["branch_id"]
        assert account.pushes[-1]["branch"]["branch_id"] == first["branch_id"]
        vault = HistoryVault()
        try:
            assert vault.branch_named("vase", first["kept_as"])["created_at"] == made_at
        finally:
            vault.close()
