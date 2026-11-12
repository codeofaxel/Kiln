"""A design folder on this computer is synced with one design in one Kiln account.

Two Kiln accounts that each hold a design of the same name, used on one
computer, are never mixed: once a folder is tied to one account's design by a
push, pull or clone, the other account's design of that name is refused
before anything here changes, and no version is renumbered for it.  Driven
through the history courier (a plain install), against a stand-in for the
two accounts that answers the way Kiln's servers do: each design named by
its own opaque key, and a push carrying another design's tie refused before
anything of it is kept.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from kiln import design_tie
from kiln import history_courier as courier
from kiln.history_vault import HistoryVault

KEY_X = "k1:" + "a" * 64
KEY_Y = "k1:" + "b" * 64


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("KILN_DESIGNS_DIR", raising=False)
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    return tmp_path


class _Accounts:
    """Two Kiln accounts, X and Y, each holding its own design ``vase``.

    ``signed_in`` picks whose account answers.  A push carrying a tie that is
    not the signed-in account's design is refused the way Kiln's servers
    refuse it (HTTP 412, nothing kept), and every push that was kept is
    recorded in ``kept``.
    """

    def __init__(self) -> None:
        self.signed_in = "X"
        self.designs = {
            "X": {"key": KEY_X, "versions": [("x" * 31 + str(n), n) for n in (1, 2, 3)]},
            "Y": {"key": KEY_Y, "versions": [("y" * 31 + str(n), n) for n in (1, 2)]},
        }
        self.kept: list[dict] = []
        self.models_sent: list[dict] = []

    def _bundle(self, *, single: bool) -> dict:
        own = self.designs[self.signed_in]
        ids = [vid for vid, _n in own["versions"]]
        branch = {"branch_id": f"{self.signed_in}-main", "design_id": "vase", "name": "main",
                  "head_version_id": ids[-1]}
        versions = [{"version_id": vid, "parent_version_id": ids[i - 1] if i else None,
                     "created_at": 10.0 + i, "version_number": n}
                    for i, (vid, n) in enumerate(own["versions"])]
        answer = {
            "versions": versions,
            "memberships": [{"branch_id": branch["branch_id"], "version_id": vid, "position": i}
                            for i, vid in enumerate(ids)],
            "provenance_sidecars": [{"version_id": vid, "recipe_dict": {
                "name": "vase", "design_id": "vase", "version": n, "version_id": vid,
                "notes": f"{self.signed_in} v{n}", "source_scad": f"cube({n});",
                "mesh_bytes_b64": base64.b64encode(f"solid {vid}\nendsolid\n".encode()).decode(),
                "created": "2026-11-01T00:00:00+00:00"}} for vid, n in own["versions"]],
            "releases": [],
            "account_numbers": {vid: n for vid, n in own["versions"]},
            "version_number_mark": max(n for _vid, n in own["versions"]),
            "design_key": own["key"],
        }
        if single:
            answer["branch"] = branch
        else:
            answer["branches"] = [branch]
        return answer

    def request(self, tool, method, path, body=None, **_kw):
        own = self.designs[self.signed_in]
        if path.endswith("/clone"):
            return self._bundle(single=False)
        if path.endswith("/branches/pull"):
            return self._bundle(single=True)
        if path.endswith("/branches/push"):
            carried = (body or {}).get("design_key")
            if carried and carried != own["key"]:
                return {**design_tie.refusal("vase", "push"), "http_status": 412}
            self.kept.append({"account": self.signed_in, "body": body})
            ids = [v["version_id"] for v in body["versions"]]
            held = {vid for vid, _n in own["versions"]}
            new = [vid for vid in ids if vid not in held]
            numbers = {vid: n for vid, n in own["versions"]}
            mark = max(numbers.values(), default=0)
            for vid in new:
                mark += 1
                numbers[vid] = mark
                own["versions"].append((vid, mark))
            return {"pushed_versions": new, "skipped_versions": [v for v in ids if v in held],
                    "version_numbers": {vid: numbers[vid] for vid in ids},
                    "account_numbers": numbers, "version_number_mark": mark, "design_key": own["key"]}
        if path.endswith("/proven"):
            return {"recipes": []}
        if path.endswith("/proven/push"):
            return {"kept": 0}
        if path.endswith("/meshes/stored"):
            carried = (body or {}).get("design_key")
            if carried and carried != own["key"]:
                return {**design_tie.refusal("vase", "push"), "http_status": 412}
            return {"stored": []}
        if method == "PUT" and "/api/cloud/meshes/" in path:
            self.models_sent.append({"account": self.signed_in, "path": path})
            return {"stored": True}
        return {"status": "error", "code": "NOT_EMULATED", "error": path}


@pytest.fixture
def accounts(home, monkeypatch):
    stand_in = _Accounts()
    monkeypatch.setattr(courier, "_request", stand_in.request)
    return stand_in


def _folder(home: Path) -> Path:
    return home / ".kiln" / "designs" / "vase"


def _snapshot(folder: Path) -> dict[str, str]:
    return {p.name: p.read_text() for p in sorted(folder.iterdir()) if p.is_file()}


def _records() -> dict[str, int | None]:
    vault = HistoryVault()
    try:
        return {vid: (vault.version(vid) or {}).get("recipe", {}).get("version")
                for vid in vault.version_ids("vase")}
    finally:
        vault.close()


def _recipe(folder: Path, number: int, vid: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f".kiln_recipe.v{number}.json").write_text(json.dumps({
        "name": "vase", "design_id": "vase", "version": number, "version_id": vid,
        "source_scad": f"cube({number});", "created": "2026-11-01T00:00:00+00:00",
    }), encoding="utf-8")


class TestTwoAccountsWithOneDesignName:
    def test_a_clone_ties_the_folder_and_the_other_accounts_pull_and_clone_are_refused(self, home, accounts):
        cloned = courier.clone("cloud_clone_design", "vase")
        assert cloned["status"] == "success", cloned
        folder = _folder(home)
        assert design_tie.tied_key(folder) == KEY_X
        before, records = _snapshot(folder), _records()
        assert sorted(records.values()) == [1, 2, 3]

        accounts.signed_in = "Y"
        for doing, call in (("pull", lambda: courier.pull("cloud_pull_branch", "vase")),
                            ("clone", lambda: courier.clone("cloud_clone_design", "vase"))):
            refused = call()
            assert refused["code"] == design_tie.REFUSED_CODE, refused
            assert refused["changed"] is False
            assert _snapshot(folder) == before, f"a refused {doing} changed the folder"
            assert _records() == records, f"a refused {doing} changed this computer's records"

        accounts.signed_in = "X"
        again = courier.pull("cloud_pull_branch", "vase")
        assert again["status"] == "success", again
        assert sorted(_records().values()) == [1, 2, 3], "X's versions were renumbered"

    def test_a_push_into_the_other_accounts_design_is_refused_and_nothing_is_kept(self, home, accounts):
        assert courier.clone("cloud_clone_design", "vase")["status"] == "success"
        folder = _folder(home)
        before, records = _snapshot(folder), _records()

        accounts.signed_in = "Y"
        refused = courier.push("cloud_push_branch", "vase")
        assert refused["code"] == design_tie.REFUSED_CODE, refused
        assert refused["error"] == design_tie.refusal("vase", "push", tool="cloud_push_branch")["error"]
        assert not [k for k in accounts.kept if k["account"] == "Y"], "Y's account kept the push"
        assert not [m for m in accounts.models_sent if m["account"] == "Y"], (
            "a model of the folder was sent to Y's account before the push was refused"
        )
        assert _snapshot(folder) == before and _records() == records

    def test_the_tie_and_the_refusal_name_nothing_of_either_account(self, home, accounts):
        assert courier.clone("cloud_clone_design", "vase")["status"] == "success"
        tie = json.loads((_folder(home) / design_tie.TIE_FILE).read_text())
        assert set(tie) == {design_tie.KEY_FIELD, "tied_by", "tied_at"}
        accounts.signed_in = "Y"
        said = json.dumps(courier.pull("cloud_pull_branch", "vase"))
        for detail in (KEY_X, KEY_Y, "X v", "Y v", *(vid for d in accounts.designs.values()
                                                       for vid, _n in d["versions"])):
            assert detail not in said, f"the refusal names {detail!r}"
        assert "a different Kiln account" in said


class TestAFolderTiedToNoDesignYet:
    def test_versions_the_account_also_holds_tie_it_to_that_design_quietly(self, home, accounts):
        folder = _folder(home)
        for vid, n in accounts.designs["X"]["versions"][:2]:
            _recipe(folder, n, vid)
        pulled = courier.pull("cloud_pull_branch", "vase")
        assert pulled["status"] == "success", pulled
        assert design_tie.tied_key(folder) == KEY_X
        assert "said_tie" not in pulled

        accounts.signed_in = "Y"
        assert courier.pull("cloud_pull_branch", "vase")["code"] == design_tie.REFUSED_CODE

    def test_versions_the_account_does_not_hold_tie_it_and_the_answer_says_so(self, home, accounts):
        folder = _folder(home)
        for n in (1, 2):
            _recipe(folder, n, "here" + str(n) * 28)
        pulled = courier.pull("cloud_pull_branch", "vase")
        assert pulled["status"] == "success", pulled
        assert design_tie.tied_key(folder) == KEY_X
        assert "now synced with vase in this Kiln account" in pulled["said_tie"]

    def test_a_push_from_an_untied_folder_ties_it_to_the_design_the_account_kept(self, home, accounts):
        folder = _folder(home)
        _recipe(folder, 1, "solo" + "1" * 28)
        accounts.designs["X"]["versions"] = []
        pushed = courier.push("cloud_push_branch", "vase")
        assert pushed["status"] == "success", pushed
        assert design_tie.tied_key(folder) == KEY_X
        assert "said_tie" not in pushed, "a first push of a new design is no surprise"


class TestOwnerAndContributorOnOneComputer:
    def test_a_contributors_served_change_leaves_the_owners_tie_and_pull_as_they_were(self, home, accounts):
        """The owner's folder is tied to the owner's design.  A contributor on
        the same computer changes the shared design through a served call:
        the change lands here as the owner's design's history, the tie stays,
        and the owner's next pull is not refused."""
        assert courier.clone("cloud_clone_design", "vase")["status"] == "success"
        folder = _folder(home)
        head = accounts.designs["X"]["versions"][-1][0]
        made = "c" * 32
        delta = {
            "design_id": "vase",
            "versions": [{"version_id": made, "parent_version_id": head, "created_at": 99.0}],
            "provenance_sidecars": [{"version_id": made, "recipe_dict": {
                "name": "vase", "design_id": "vase", "version": 4, "version_id": made,
                "notes": "by the contributor", "created": "2026-11-02T00:00:00+00:00"}}],
            "branches": [{"branch_id": "X-main", "design_id": "vase", "name": "main", "head_version_id": made}],
            "memberships": [{"branch_id": "X-main", "version_id": made, "position": 3}],
            "expected_heads": {"X-main": head},
        }
        courier.take_delta({"history_delta": {"designs": [delta]}}, {"named": ["vase"]})
        assert design_tie.tied_key(folder) == KEY_X
        assert made in _records(), "the served change was not kept here"

        accounts.designs["X"]["versions"].append((made, 4))
        pulled = courier.pull("cloud_pull_branch", "vase")
        assert pulled["status"] == "success", pulled
        assert design_tie.tied_key(folder) == KEY_X


class TestNeverOnKilnsServer:
    def test_nothing_is_judged_or_written_on_the_shared_server(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        folder = tmp_path / "vase"
        folder.mkdir()
        (folder / design_tie.TIE_FILE).write_text(json.dumps({design_tie.KEY_FIELD: KEY_X}))
        verdict = design_tie.judge("vase", folder, {"design_key": KEY_Y}, doing="pull")
        assert verdict.refused is None
        assert design_tie.keep(verdict) is None
        assert design_tie.push_fields(folder) == {}
        assert design_tie.numbers_may_be_taken(folder, {"design_key": KEY_Y})


class TestNumbersFollowTheTie:
    def test_a_tied_folder_takes_numbers_only_from_its_own_design(self, home, accounts):
        assert courier.clone("cloud_clone_design", "vase")["status"] == "success"
        folder = _folder(home)
        x1 = accounts.designs["X"]["versions"][0][0]
        before = _records()
        vault = HistoryVault()
        try:
            for answer in ({"account_numbers": {x1: 7, "other": 1}, "design_key": KEY_Y},
                           {"account_numbers": {x1: 7, "other": 1}}):
                taken = vault.take_account_numbers("vase", answer)
                assert taken == {"said": design_tie.NUMBERS_NOT_FOR_THIS_FOLDER}
        finally:
            vault.close()
        assert _records() == before
        assert design_tie.numbers_may_be_taken(folder, {"design_key": KEY_X})
