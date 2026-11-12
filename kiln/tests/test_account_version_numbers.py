"""A Kiln account's version numbers, taken on this computer.

The account gives each version its number once and never changes it.  A
computer takes the account's number for every version the account holds,
everywhere it records one, and moves a version only it holds out of the way
of a number the account gave another version.  An answer that keeps no
numbers changes nothing: a number is never worked out from a count.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiln import history_courier as courier
from kiln import version_numbers as vn
from kiln.history_vault import HistoryVault


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("KILN_DESIGNS_DIR", raising=False)
    return tmp_path


def _recipe(folder: Path, number: int, vid: str | None, **extra) -> Path:
    data = {"name": folder.name, "design_id": folder.name, "version": number,
            "created": f"2026-11-0{min(number, 9)}T12:00:00+00:00", **extra}
    if vid:
        data["version_id"] = vid
    path = folder / f".kiln_recipe.v{number}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _numbers(folder: Path) -> dict[str, int]:
    return {
        json.loads(p.read_text()).get("version_id", p.name): json.loads(p.read_text())["version"]
        for p in folder.glob(".kiln_recipe.v*.json")
    }


class TestThePlan:
    def test_a_version_the_account_holds_takes_its_number(self):
        assert vn.plan_numbers({"a": 1, "b": 2}, {"a": 1, "b": 5}) == {"a": 1, "b": 5}

    def test_a_version_only_here_moves_past_a_number_the_account_gave_another(self):
        final = vn.plan_numbers({"a": 1, "mine": 3}, {"a": 1, "theirs": 3}, past=[3])
        assert final == {"a": 1, "mine": 4}

    def test_it_moves_past_every_number_ever_given(self):
        final = vn.plan_numbers({"mine": 3}, {"theirs": 3}, past=[9])
        assert final == {"mine": 10}

    def test_a_version_only_here_that_collides_with_nothing_keeps_its_number(self):
        assert vn.plan_numbers({"mine": 7}, {"theirs": 3}) == {"mine": 7}

    def test_no_two_versions_end_with_one_number(self):
        final = vn.plan_numbers({"a": 2, "b": 5, "c": 5}, {"a": 5, "x": 6})
        assert final["a"] == 5
        assert len(set(final.values())) == len(final)
        assert 6 not in final.values()


class TestTheAnswer:
    def test_only_what_the_account_said_is_read(self):
        assert vn.account_numbers_in({"pushed_versions": ["a", "b"]}) == ({}, 0)
        assert vn.account_numbers_in({"account_numbers": {"a": 1, "b": "2", "c": True}}) == ({"a": 1}, 0)
        answer = {"versions": [{"version_id": "p", "version_number": 3}], "version_number_mark": 4}
        assert vn.account_numbers_in(answer) == ({"p": 3}, 4)


class TestTheFolder:
    def test_a_version_moves_with_its_head_its_children_and_the_mark(self, tmp_path):
        folder = tmp_path / "mug"
        folder.mkdir()
        _recipe(folder, 1, "a")
        two = _recipe(folder, 2, "b")
        _recipe(folder, 3, "c", parent_version=str(two))
        (folder / ".kiln_recipe.json").write_text(json.dumps({"version": 2, "version_id": "b", "name": "mug"}))
        changes = vn.renumber_folder(folder, {"a": 1, "b": 5, "c": 3})
        assert changes == [{"key": "b", "from": 2, "to": 5}]
        assert _numbers(folder) == {"a": 1, "b": 5, "c": 3}
        assert not (folder / ".kiln_recipe.v2.json").exists()
        assert json.loads((folder / ".kiln_recipe.json").read_text())["version"] == 5
        child = json.loads((folder / ".kiln_recipe.v3.json").read_text())
        assert Path(child["parent_version"]).name == ".kiln_recipe.v5.json"
        assert vn.next_version_number(folder) == 6

    def test_a_versions_proven_recipes_move_with_it(self, tmp_path):
        folder = tmp_path / "mug"
        folder.mkdir()
        _recipe(folder, 1, "a")
        _recipe(folder, 2, "b")
        proven = {"recipes": [{"decoration_type": "emboss", "product_type": "mug"}]}
        (folder / ".kiln_proven_recipe.v2.json").write_text(json.dumps(proven))
        (folder / ".kiln_proven_recipe.v1.json").write_text(json.dumps({"recipes": []}))
        vn.renumber_folder(folder, {"a": 1, "b": 5})
        assert not (folder / ".kiln_proven_recipe.v2.json").exists()
        assert json.loads((folder / ".kiln_proven_recipe.v5.json").read_text()) == proven
        assert json.loads((folder / ".kiln_proven_recipe.v1.json").read_text()) == {"recipes": []}

    def test_two_versions_swap_without_one_writing_over_the_other(self, tmp_path):
        folder = tmp_path / "cup"
        folder.mkdir()
        _recipe(folder, 1, "a")
        _recipe(folder, 2, "b")
        vn.renumber_folder(folder, {"a": 2, "b": 1})
        assert _numbers(folder) == {"a": 2, "b": 1}
        assert sorted(p.name for p in folder.glob(".kiln_recipe*.json")) == [
            ".kiln_recipe.v1.json", ".kiln_recipe.v2.json"]


class TestTheStore:
    def test_rows_take_the_numbers_in_one_go_and_never_share_one(self, tmp_path):
        from kiln.design_versions import DesignVersionStore

        store = DesignVersionStore(db_path=str(tmp_path / "v.db"))
        try:
            a = store.save_version("mug", "cube(1);")
            b = store.save_version("mug", "cube(2);")
            assert store.set_numbers("mug", {a.version_id: 1, b.version_id: 5}) == [(b.version_id, 2, 5)]
            assert {v.version_id: v.version_number for v in store.list_versions("mug")} == {
                a.version_id: 1, b.version_id: 5}
            assert max(store.numbers("mug")) == 5
            with pytest.raises(ValueError):
                store.set_numbers("mug", {b.version_id: 1})
            assert store.get_version(b.version_id).version_number == 5
        finally:
            store.close()


class TestAPlainInstall:
    def test_a_pull_moves_a_version_only_here_out_of_the_accounts_way(self, home):
        """This computer saved a version 3 nobody else has; the account gave
        3 to another computer's version.  The pull files the pulled one as
        version 3 and moves this computer's own past it, in the records and
        the library."""
        folder = home / ".kiln" / "designs" / "vase"
        folder.mkdir(parents=True)
        for n in (1, 2, 3):
            _recipe(folder, n, None, source_scad=f"cube({n});", parts=[])
        vault = HistoryVault()
        try:
            vault.sync_from_library("vase")
            mine = {json.loads(p.read_text())["version"]: json.loads(p.read_text())["version_id"]
                    for p in folder.glob(".kiln_recipe.v*.json")}
            theirs = "t" * 32
            answer = {
                "design_id": "vase",
                "branch": {"branch_id": "remote", "design_id": "vase", "name": "theirs",
                           "head_version_id": theirs},
                "versions": [{"version_id": theirs, "parent_version_id": mine[2], "created_at": 9.0,
                              "version_number": 3}],
                "memberships": [{"branch_id": "remote", "version_id": theirs, "position": 0}],
                "provenance_sidecars": [{"version_id": theirs, "recipe_dict": {
                    "name": "vase", "design_id": "vase", "version": 3, "created": "2026-11-09T00:00:00+00:00"}}],
                "account_numbers": {mine[1]: 1, mine[2]: 2, theirs: 3},
                "version_number_mark": 3,
            }
            applied = vault.apply_remote(answer)
        finally:
            vault.close()
        numbers = _numbers(folder)
        assert numbers[theirs] == 3, "the pulled version is filed under its account number"
        assert numbers[mine[3]] == 4, "this computer's own version 3 moved past the account's"
        assert "version 3 is now version 4" in applied["numbers_here"]["said"]
        vault = HistoryVault()
        try:
            assert vault.version(mine[3])["recipe"]["version"] == 4
        finally:
            vault.close()

    def test_a_pull_numbers_the_next_save_past_every_number_the_account_gave(self, home):
        """The account holds numbers up to 12 on branches this pull did not
        bring: this computer's next version is 13, never a number another
        computer already took."""
        folder = home / ".kiln" / "designs" / "lamp"
        folder.mkdir(parents=True)
        _recipe(folder, 1, None, source_scad="cube(1);", parts=[])
        vault = HistoryVault()
        try:
            vault.sync_from_library("lamp")
            mine = json.loads((folder / ".kiln_recipe.v1.json").read_text())["version_id"]
            theirs = "p" * 32
            vault.apply_remote({
                "design_id": "lamp",
                "branch": {"branch_id": "remote", "design_id": "lamp", "name": "theirs",
                           "head_version_id": theirs},
                "versions": [{"version_id": theirs, "parent_version_id": mine, "created_at": 9.0,
                              "version_number": 9}],
                "memberships": [{"branch_id": "remote", "version_id": theirs, "position": 0}],
                "provenance_sidecars": [{"version_id": theirs, "recipe_dict": {
                    "name": "lamp", "design_id": "lamp", "version": 9, "created": "2026-11-09T00:00:00+00:00"}}],
                "account_numbers": {mine: 1, theirs: 9, "elsewhere-12": 12},
                "version_number_mark": 12,
            })
        finally:
            vault.close()
        assert vn.next_version_number(folder) == 13

    def test_a_push_takes_the_numbers_the_account_gave(self, home, monkeypatch):
        folder = home / ".kiln" / "designs" / "mug"
        folder.mkdir(parents=True)
        for n in (1, 2):
            _recipe(folder, n, None, source_scad=f"cube({n});", parts=[])
        # A recipe proven on version 2 of this computer.
        (folder / ".kiln_proven_recipe.v2.json").write_text(json.dumps(
            {"recipes": [{"decoration_type": "emboss", "product_type": "mug"}]}))
        proven_sent: list[dict] = []

        def account(tool, method, path, body=None, **_kw):
            if path.endswith("/branches/push"):
                ids = [v["version_id"] for v in body["versions"]]
                numbers = {ids[0]: 1, ids[1]: 5}
                return {"pushed_versions": ids, "version_numbers": numbers,
                        "account_numbers": {**numbers, "other-2": 2, "other-3": 3, "other-4": 4},
                        "version_number_mark": 5}
            if path.endswith("/proven/push"):
                proven_sent.extend(body["recipes"])
                return {"kept": len(body["recipes"])}
            return {"stored": []}

        monkeypatch.setattr(courier, "_request", account)
        answer = courier.push("cloud_push_branch", "mug")
        assert answer["status"] == "success"
        assert sorted(_numbers(folder).values()) == [1, 5]
        assert "version 2 is now version 5" in answer["said_numbers"]
        assert vn.next_version_number(folder) == 6
        # The proven recipe reached the account under its version's number there.
        assert [r["version_ref"] for r in proven_sent] == ["5"]


class TestHeadsAndFilesThatNameNoVersion:
    def test_a_head_that_names_no_version_follows_the_version_it_copies(self, tmp_path):
        """A plain save writes a head that names no version.  A push moved
        that version from 4 to 5 and the head still said 4, so the next save
        was diffed against a version 4 no file holds."""
        folder = tmp_path / "vase"
        folder.mkdir()
        _recipe(folder, 1, "a", source_scad="cube(1);")
        four = _recipe(folder, 4, "b", source_scad="sphere(4);")
        head = json.loads(four.read_text())
        head.pop("version_id")
        (folder / ".kiln_recipe.json").write_text(json.dumps(head))

        vn.renumber_folder(folder, {"a": 1, "b": 5})

        after = json.loads((folder / ".kiln_recipe.json").read_text())
        assert (after["version"], after["version_id"]) == (5, "b")

    def test_a_head_that_copies_a_version_that_did_not_move_stays_as_it_is(self, tmp_path):
        folder = tmp_path / "vase"
        folder.mkdir()
        one = _recipe(folder, 1, "a", source_scad="cube(1);")
        _recipe(folder, 2, "b", source_scad="sphere(2);")
        head = json.loads(one.read_text())
        head.pop("version_id")
        (folder / ".kiln_recipe.json").write_text(json.dumps(head))

        vn.renumber_folder(folder, {"a": 1, "b": 5})

        assert json.loads((folder / ".kiln_recipe.json").read_text()) == head

    def test_a_kept_file_that_names_no_version_is_never_moved_for_naming_none(self):
        """The account agrees with this computer: version 2 is 2.  Its kept
        file is from before files named their version, so it counted as a
        second version holding 2 and was moved past it: a push renamed the
        version's only record and said the account had given its number away."""
        unnamed = vn.FILE_KEY + ".kiln_recipe.v2.json"
        final = vn.plan_numbers({unnamed: 2, "a": 1, "b": 2, "mine": 3}, {"a": 1, "b": 2, "theirs": 3})
        assert final[unnamed] == 2
        assert final["mine"] == 4, "a version moved past the numbers given goes past the unnamed file too"


class TestTheFilesMoveWholeOrNotAtAll:
    def test_a_failure_part_way_puts_every_file_back(self, tmp_path, monkeypatch):
        folder = tmp_path / "cup"
        folder.mkdir()
        for n, vid in ((1, "a"), (2, "b"), (3, "c")):
            _recipe(folder, n, vid)
        before = {p.name: p.read_text() for p in folder.glob(".kiln_recipe*.json")}
        real = vn._write_json
        writes: list[Path] = []

        def fails_second(path, data):
            writes.append(path)
            if len(writes) == 2:
                raise OSError("the disk is full")
            real(path, data)

        monkeypatch.setattr(vn, "_write_json", fails_second)
        with pytest.raises(OSError):
            vn.renumber_folder(folder, {"a": 4, "b": 5, "c": 6})

        assert {p.name: p.read_text() for p in folder.glob(".kiln_recipe*.json")} == before


def _pulled_answer(design_id: str, mine: dict[int, str], theirs: str, number: int) -> dict:
    return {
        "design_id": design_id,
        "branch": {"branch_id": "remote", "design_id": design_id, "name": "theirs", "head_version_id": theirs},
        "versions": [{"version_id": theirs, "parent_version_id": mine[1], "created_at": 9.0,
                      "version_number": number}],
        "memberships": [{"branch_id": "remote", "version_id": theirs, "position": 0}],
        "provenance_sidecars": [{"version_id": theirs, "recipe_dict": {
            "name": design_id, "design_id": design_id, "version": number, "version_id": theirs,
            "created": "2026-11-09T00:00:00+00:00"}}],
        "account_numbers": {mine[1]: 1, theirs: number},
        "version_number_mark": number,
    }


def _synced(home: Path, design_id: str, count: int) -> tuple[Path, dict[int, str]]:
    folder = home / ".kiln" / "designs" / design_id
    folder.mkdir(parents=True)
    for n in range(1, count + 1):
        _recipe(folder, n, None, source_scad=f"cube({n});", parts=[])
    vault = HistoryVault()
    try:
        vault.sync_from_library(design_id)
    finally:
        vault.close()
    return folder, {json.loads(p.read_text())["version"]: json.loads(p.read_text())["version_id"]
                    for p in folder.glob(".kiln_recipe.v*.json")}


class TestASaveWhileTheNumbersAreTaken:
    def test_a_save_during_a_pull_waits_and_takes_the_next_number(self, home, monkeypatch):
        """A save landed between the pull's plan and its files moving: it
        took the number the plan gave a version here, and two files said
        one number."""
        import threading
        import time

        folder, mine = _synced(home, "vase", 2)
        started = threading.Event()
        taken: list[int] = []
        real = vn.renumber_folder

        def slow(where, final, **kw):
            started.set()
            time.sleep(0.5)
            return real(where, final, **kw)

        def save() -> None:
            started.wait(5)
            n = vn.reserve_version_number(folder)
            (folder / f".kiln_recipe.v{n}.json").write_text(json.dumps(
                {"name": "vase", "version": n, "version_id": "racer", "created": "2026-11-11T00:00:00+00:00"}))
            taken.append(n)

        monkeypatch.setattr(vn, "renumber_folder", slow)
        racer = threading.Thread(target=save)
        racer.start()
        vault = HistoryVault()
        try:
            vault.take_account_numbers("vase", {"account_numbers": {mine[1]: 1, "theirs": 2}, "version_number_mark": 2})
        finally:
            vault.close()
            racer.join(10)

        numbers = sorted(json.loads(p.read_text())["version"] for p in folder.glob(".kiln_recipe*.json")
                         if p.name != ".kiln_recipe.json")
        assert len(numbers) == len(set(numbers)), numbers
        assert taken == [4]

    def test_a_pull_whose_numbers_cannot_be_taken_lays_nothing_down(self, home, monkeypatch):
        import fcntl

        folder, mine = _synced(home, "vase", 2)
        before = {p.name: p.read_text() for p in folder.glob(".kiln_recipe*.json")}
        monkeypatch.setattr(vn, "_LOCK_WAIT_SECONDS", 0.2)
        monkeypatch.setattr(courier, "_request",
                            lambda tool, method, path, body=None, **kw: _pulled_answer("vase", mine, "t" * 32, 3))
        holder = open(folder / ".kiln_numbers.lock", "a+")
        try:
            fcntl.flock(holder, fcntl.LOCK_EX)
            answer = courier.pull("cloud_pull_branch", "vase")
        finally:
            holder.close()

        assert answer["status"] == "error" and answer["code"] == "NUMBERS_NOT_TAKEN", answer
        assert "nothing here changed" in answer["error"]
        assert {p.name: p.read_text() for p in folder.glob(".kiln_recipe*.json")} == before
        vault = HistoryVault()
        try:
            assert vault.version("t" * 32) is None
        finally:
            vault.close()

    def test_a_push_whose_numbers_cannot_be_taken_still_says_so(self, home, monkeypatch):
        import fcntl

        folder, mine = _synced(home, "mug", 2)

        def account(tool, method, path, body=None, **_kw):
            if path.endswith("/branches/push"):
                ids = [v["version_id"] for v in body["versions"]]
                return {"pushed_versions": ids, "version_numbers": {ids[0]: 1, ids[1]: 5},
                        "account_numbers": {ids[0]: 1, ids[1]: 5, "other-2": 2}, "version_number_mark": 5}
            return {"stored": []}

        monkeypatch.setattr(courier, "_request", account)
        monkeypatch.setattr(vn, "_LOCK_WAIT_SECONDS", 0.2)
        holder = open(folder / ".kiln_numbers.lock", "a+")
        try:
            fcntl.flock(holder, fcntl.LOCK_EX)
            answer = courier.push("cloud_push_branch", "mug")
        finally:
            holder.close()

        assert answer["status"] == "success", answer
        assert "could not be taken" in answer["said_numbers"]
        assert sorted(_numbers(folder).values()) == [1, 2], "nothing here changed"
