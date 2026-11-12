"""A save and a pull of one design never write between each other's steps.

A save takes its version's number, then writes the version's file.  A pull
plans the account's numbers, then writes the pulled versions' files.  The
case set up here: a save takes number 4 while a pull lays down the account's
version 4 (made on another computer); the save must not write its version
over that file.

Three things hold that, each proven below:

* every door that makes a version holds the design's numbers lock from
  taking the number until the file is written, and a pull holds it from
  planning until its files are written;
* the lock is a real one on every system: the system's file lock (POSIX or
  Windows), else a file only one holder can make, and it holds between the
  threads of one process;
* the recipe writer itself never writes a version's kept file over another
  version's, whoever calls it.
"""

from __future__ import annotations

import ast
import builtins
import json
import sys
import threading
import time
from pathlib import Path

import pytest

from kiln import design_recipe, version_numbers
from kiln import history_courier as courier
from kiln.design_recipe import create_recipe, save_recipe
from tests.test_design_tie import _Accounts, _folder, home  # noqa: F401 -- fixture

SRC = Path(version_numbers.__file__).resolve().parent


def _kept(folder: Path) -> dict[int, dict]:
    """Each kept version file in *folder*, by the number in its name."""
    out = {}
    for path in folder.glob(".kiln_recipe.v*.json"):
        out[int(path.name.split(".v")[1].split(".")[0])] = json.loads(path.read_text())
    return out


def _numbers_agree(folder: Path) -> None:
    for number, data in _kept(folder).items():
        assert data["version"] == number, f"the file named v{number} says version {data['version']}"


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------


class TestTheWriterNeverWritesOverAnotherVersion:
    def _pulled(self, folder: Path, number: int) -> dict:
        folder.mkdir(parents=True, exist_ok=True)
        data = {"name": "vase", "design_id": "vase", "version": number, "version_id": "a" * 32,
                "source_scad": "cube(4);", "notes": "made on another computer",
                "created": "2026-01-01T00:00:00+00:00"}
        (folder / f".kiln_recipe.v{number}.json").write_text(json.dumps(data))
        return data

    def test_a_number_another_versions_file_holds_is_left_and_the_next_is_taken(self, home):
        folder = _folder(home)
        pulled = self._pulled(folder, 4)
        recipe = create_recipe(name="vase", parts=[], source_scad="sphere(4);", notes="saved here")
        recipe.design_id, recipe.version = "vase", 4

        save_recipe(recipe, str(folder))

        kept = _kept(folder)
        assert kept[4] == pulled, "the other version's file was written over"
        assert recipe.version == 5 and kept[5]["notes"] == "saved here"
        assert json.loads((folder / ".kiln_recipe.json").read_text())["version"] == 5
        assert version_numbers.next_version_number(folder) == 6, "the number taken was not written down"

    def test_a_version_written_again_keeps_its_number_and_the_id_its_file_names(self, home):
        folder = _folder(home)
        folder.mkdir(parents=True)
        recipe = create_recipe(name="vase", parts=[], source_scad="cube(1);", notes="first")
        recipe.design_id = "vase"
        save_recipe(recipe, str(folder))
        named = {**json.loads((folder / ".kiln_recipe.v1.json").read_text()), "version_id": "b" * 32}
        (folder / ".kiln_recipe.v1.json").write_text(json.dumps(named))

        again = design_recipe.load_recipe(str(folder))
        again.notes = "the same version, written again"
        save_recipe(again, str(folder))

        assert sorted(_kept(folder)) == [1] and again.version == 1
        assert _kept(folder)[1]["notes"] == "the same version, written again"
        assert _kept(folder)[1]["version_id"] == "b" * 32, "writing a version again dropped the id its file named"


# ---------------------------------------------------------------------------
# A save in flight, and a pull
# ---------------------------------------------------------------------------


def _save_tool():
    from kiln.plugins.version_tools import _VersionToolsPlugin

    tools: dict = {}

    class _MCP:
        def tool(self, **_kw):
            def deco(fn):
                tools[fn.__name__] = fn
                return fn
            return deco

    _VersionToolsPlugin().register(_MCP())
    return tools


@pytest.fixture
def accounts(home, monkeypatch):  # noqa: F811
    stand_in = _Accounts()
    monkeypatch.setattr(courier, "_request", stand_in.request)
    # No kiln-pro in these: the save is a plain install's.
    real_import = builtins.__import__

    def no_pro(name, *args, **kwargs):
        if name.split(".")[0] == "kiln_pro":
            raise ImportError("No module named 'kiln_pro'", name="kiln_pro")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pro)
    for loaded in [m for m in sys.modules if m.split(".")[0] == "kiln_pro"]:
        monkeypatch.delitem(sys.modules, loaded)
    return stand_in


class _Paused:
    """The recipe writer, stopped at its door until released: the moment
    between a save taking its number and writing its file."""

    def __init__(self, monkeypatch) -> None:
        self.reached, self.release = threading.Event(), threading.Event()
        real = design_recipe.save_recipe

        def paused(recipe, directory):
            self.number = recipe.version
            self.reached.set()
            assert self.release.wait(20), "the test never released the save"
            return real(recipe, directory)

        monkeypatch.setattr(design_recipe, "save_recipe", paused)


@pytest.mark.parametrize("door", ["save_design_version", "rollback_design_version"])
def test_a_pull_waits_for_a_save_in_flight_and_neither_writes_over_the_other(home, accounts, monkeypatch, door):
    assert courier.clone("cloud_clone_design", "vase")["status"] == "success"
    folder = _folder(home)
    # Another computer made the account's version 4 after this one cloned.
    other = "f" * 32
    accounts.designs["X"]["versions"].append((other, 4))

    tools = _save_tool()
    paused = _Paused(monkeypatch)
    answer: dict = {}
    call = (
        (lambda: tools[door](design_id="vase", scad_source="sphere(44);", notes="saved here, racing"))
        if door == "save_design_version" else (lambda: tools[door](design_id="vase", to_version_id="1"))
    )
    saving = threading.Thread(target=lambda: answer.update(call()))
    saving.start()
    try:
        assert paused.reached.wait(20), "the save never reached its writer"
        assert paused.number == 4, "the save in flight did not take the number the account's new version holds"
        before = {p.name: p.read_bytes() for p in folder.iterdir() if p.name != ".kiln_numbers.lock"}

        monkeypatch.setattr(version_numbers, "_LOCK_WAIT_SECONDS", 0.3)
        refused = courier.pull("cloud_pull_branch", "vase")
        assert refused.get("code") == "NUMBERS_NOT_TAKEN", (
            f"a pull landed between a save's number and its file: {refused}"
        )
        assert {p.name: p.read_bytes() for p in folder.iterdir() if p.name != ".kiln_numbers.lock"} == before
    finally:
        paused.release.set()
        saving.join(30)
    assert answer.get("ok"), answer

    monkeypatch.setattr(version_numbers, "_LOCK_WAIT_SECONDS", 30.0)
    pulled = courier.pull("cloud_pull_branch", "vase")
    assert pulled["status"] == "success", pulled
    kept = _kept(folder)
    _numbers_agree(folder)
    assert kept[4]["version_id"] == other, "the account's version 4 is not version 4 here"
    mine = [n for n, data in kept.items() if data.get("version_id") != other and n > 3]
    assert mine == [5], f"the version saved here should have moved to 5: {sorted(kept)}"
    assert len(kept) == 5, sorted(kept)


def test_a_save_waits_for_a_pull_in_flight(home, accounts, monkeypatch):
    """The other order: a pull has taken the account's numbers and not yet
    written its files.  A save does not take a number in between."""
    from kiln.history_vault import HistoryVault

    assert courier.clone("cloud_clone_design", "vase")["status"] == "success"
    folder = _folder(home)
    other = "f" * 32
    accounts.designs["X"]["versions"].append((other, 4))

    reached, release = threading.Event(), threading.Event()
    real = HistoryVault._write_library

    def paused(self, design_id, versions):
        reached.set()
        assert release.wait(20), "the test never released the pull"
        return real(self, design_id, versions)

    monkeypatch.setattr(HistoryVault, "_write_library", paused)
    pulled: dict = {}
    pulling = threading.Thread(target=lambda: pulled.update(courier.pull("cloud_pull_branch", "vase")))
    pulling.start()
    tools = _save_tool()
    try:
        assert reached.wait(20), "the pull never reached its writer"
        monkeypatch.setattr(version_numbers, "_LOCK_WAIT_SECONDS", 0.3)
        mid = tools["save_design_version"](design_id="vase", scad_source="sphere(44);", notes="saved mid-pull")
        assert mid.get("ok") is False and "still running" in mid.get("error", ""), (
            f"a save landed between a pull's numbers and its files: {mid}"
        )
    finally:
        release.set()
        pulling.join(30)
    assert pulled.get("status") == "success", pulled
    monkeypatch.setattr(version_numbers, "_LOCK_WAIT_SECONDS", 30.0)
    saved = tools["save_design_version"](design_id="vase", scad_source="sphere(44);", notes="saved after the pull")
    assert saved.get("ok") and saved["version"]["version"] == 5, saved
    kept = _kept(folder)
    _numbers_agree(folder)
    assert kept[4]["version_id"] == other and kept[5]["notes"] == "saved after the pull" and len(kept) == 5


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------


def _held_elsewhere(folder: Path, monkeypatch) -> bool:
    """Whether another thread is refused the lock while this one holds it."""
    monkeypatch.setattr(version_numbers, "_LOCK_WAIT_SECONDS", 0.2)
    seen: list[str] = []

    def other() -> None:
        try:
            with version_numbers.numbers_lock(folder):
                seen.append("taken")
        except version_numbers.NumbersBusy:
            seen.append("busy")

    with version_numbers.numbers_lock(folder):
        thread = threading.Thread(target=other)
        thread.start()
        thread.join(10)
    return seen == ["busy"]


class _WindowsLocks:
    """``msvcrt.locking`` as Windows keeps it: one holder of a file's first
    byte at a time, whichever handle asks."""

    LK_NBLCK, LK_UNLCK = 2, 0

    def __init__(self) -> None:
        self.held: dict[tuple[int, int], int] = {}
        self.calls: list[str] = []

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        import os

        stat = os.fstat(fd)
        key = (stat.st_dev, stat.st_ino)
        if mode == self.LK_NBLCK:
            if key in self.held:
                raise OSError(13, "Permission denied")
            self.held[key] = fd
            self.calls.append("lock")
        elif self.held.get(key) == fd:
            del self.held[key]
            self.calls.append("unlock")


def _without(monkeypatch, *modules: str) -> None:
    for name in modules:
        monkeypatch.setitem(sys.modules, name, None)  # import raises ImportError


class TestTheLock:
    def test_it_holds_between_two_threads_and_its_holder_may_ask_again(self, tmp_path, monkeypatch):
        with version_numbers.numbers_lock(tmp_path), version_numbers.numbers_lock(tmp_path):
            pass
        assert _held_elsewhere(tmp_path, monkeypatch)
        with version_numbers.numbers_lock(tmp_path):  # and it was let go
            assert len(version_numbers._in_process) == 1
        assert version_numbers._in_process == {}, "a folder's lock outlived its last holder"

    def test_two_processes_never_hold_it_together(self, tmp_path):
        import subprocess

        holder = (
            "import sys, time; sys.path.insert(0, sys.argv[2]);"
            "from kiln import version_numbers as vn\n"
            "with vn.numbers_lock(sys.argv[1]):\n"
            "    print('held', flush=True); time.sleep(30)\n"
        )
        child = subprocess.Popen([sys.executable, "-c", holder, str(tmp_path), str(SRC.parent)],
                                 stdout=subprocess.PIPE, text=True)
        try:
            assert child.stdout.readline().strip() == "held"
            started = time.monotonic()
            version_numbers_wait = version_numbers._LOCK_WAIT_SECONDS
            version_numbers._LOCK_WAIT_SECONDS = 0.3
            try:
                with pytest.raises(version_numbers.NumbersBusy), version_numbers.numbers_lock(tmp_path):
                    pass
            finally:
                version_numbers._LOCK_WAIT_SECONDS = version_numbers_wait
            assert time.monotonic() - started < 5
        finally:
            child.kill()
            child.wait(10)
        with version_numbers.numbers_lock(tmp_path):  # released when its holder ended
            pass

    def test_where_there_is_no_fcntl_it_is_the_windows_file_lock(self, tmp_path, monkeypatch):
        windows = _WindowsLocks()
        _without(monkeypatch, "fcntl")
        monkeypatch.setitem(sys.modules, "msvcrt", windows)
        monkeypatch.setattr(version_numbers, "_in_process", {})

        with version_numbers.numbers_lock(tmp_path):
            assert windows.held, "nothing was locked where there is no fcntl"
            # Another process: its own handle on the same lock file.
            take, _release = version_numbers._system_lock()
            with open(tmp_path / version_numbers._LOCK, "a+") as theirs:
                assert take(theirs) is False
        assert not windows.held and windows.calls == ["lock", "unlock"]
        monkeypatch.setattr(version_numbers, "_LOCK_WAIT_SECONDS", 0.2)
        with open(tmp_path / version_numbers._LOCK, "a+") as theirs:
            take, release = version_numbers._system_lock()
            assert take(theirs)
            with pytest.raises(version_numbers.NumbersBusy), version_numbers.numbers_lock(tmp_path):
                pass
            release(theirs)

    def test_where_the_system_has_no_file_lock_it_is_a_file_only_one_can_make(self, tmp_path, monkeypatch):
        _without(monkeypatch, "fcntl", "msvcrt")
        assert version_numbers._system_lock() is None
        marker = tmp_path / f"{version_numbers._LOCK}.held"
        with version_numbers.numbers_lock(tmp_path):
            assert marker.is_file()
            assert version_numbers._take_by_file(tmp_path) is None, "a second holder made the lock file"
        assert not marker.exists()
        assert _held_elsewhere(tmp_path, monkeypatch)

        # One left by a holder that ended without removing it: refused while
        # fresh, taken over once it is old.
        marker.write_text("")
        monkeypatch.setattr(version_numbers, "_LOCK_WAIT_SECONDS", 0.2)
        with pytest.raises(version_numbers.NumbersBusy), version_numbers.numbers_lock(tmp_path):
            pass
        monkeypatch.setattr(version_numbers, "_LOCK_STALE_SECONDS", 0.0)
        time.sleep(0.05)
        with version_numbers.numbers_lock(tmp_path):
            pass
        assert not marker.exists()

    def test_a_folder_whose_files_cannot_be_locked_is_locked_by_the_file_only_one_can_make(
        self, tmp_path, monkeypatch,
    ):
        """A mount with no file locks: the lock call fails for a reason that
        is not "another holder has it".  A save is not refused as busy; it
        holds the folder's lock the other way."""
        import errno
        import fcntl

        def unsupported(_handle, _flags):
            raise OSError(errno.ENOTSUP, "Operation not supported")

        monkeypatch.setattr(fcntl, "flock", unsupported)
        monkeypatch.setattr(version_numbers, "_LOCK_WAIT_SECONDS", 2.0)
        marker = tmp_path / f"{version_numbers._LOCK}.held"
        started = time.monotonic()
        with version_numbers.numbers_lock(tmp_path):
            assert time.monotonic() - started < 1.0, "the save waited on a lock nobody holds"
            assert marker.is_file()
            assert version_numbers._take_by_file(tmp_path) is None
        assert not marker.exists()
        recipe = create_recipe(name="vase", parts=[], source_scad="cube(1);")
        save_recipe(recipe, str(tmp_path))
        assert (tmp_path / ".kiln_recipe.v1.json").is_file()

    def test_a_pull_that_cannot_get_it_says_nothing_changed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(version_numbers, "_LOCK_WAIT_SECONDS", 0.2)
        refused: list[BaseException] = []

        def pull() -> None:
            try:
                with version_numbers.pull_lock(tmp_path):
                    pass
            except version_numbers.NumbersNotTaken as exc:
                refused.append(exc)

        with version_numbers.numbers_lock(tmp_path):
            thread = threading.Thread(target=pull)
            thread.start()
            thread.join(10)
        assert refused and "nothing here changed" in str(refused[0])
        with version_numbers.pull_lock(tmp_path / "not-made-yet"):  # no folder: nothing to hold
            pass


# ---------------------------------------------------------------------------
# Every door
# ---------------------------------------------------------------------------


def _functions_calling(names: set[str]) -> list[tuple[str, str, ast.AST]]:
    """``(file, function, node)`` for each function whose own body (not a
    function nested in it) calls one of *names*."""
    found = []

    class _Calls(ast.NodeVisitor):
        def __init__(self, path: str) -> None:
            self.path, self.stack = path, []

        def _function(self, node: ast.AST) -> None:
            self.stack.append(node)
            self.generic_visit(node)
            self.stack.pop()

        visit_FunctionDef = visit_AsyncFunctionDef = _function

        def visit_Call(self, node: ast.Call) -> None:
            name = getattr(node.func, "id", getattr(node.func, "attr", None))
            if name in names and self.stack and (self.path, self.stack[-1].name, self.stack[-1]) not in found:
                found.append((self.path, self.stack[-1].name, self.stack[-1]))
            self.generic_visit(node)

    for path in sorted(SRC.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if any(name in source for name in names):
            _Calls(str(path.relative_to(SRC))).visit(ast.parse(source))
    return found


def test_every_door_that_takes_a_number_and_writes_a_file_holds_the_lock_across_both():
    """A function that takes a version's number (or asks for a new version,
    which takes one) and is not the number-taker itself must name the
    design's numbers lock: held from the number to the file."""
    takers = {
        ("version_numbers.py", "reserve_version_number"), ("design_recipe.py", "create_new_version"),
    }
    doors = [
        (path, name, func) for path, name, func in _functions_calling({"reserve_version_number", "create_new_version"})
        if (path, name) not in takers
    ]
    assert {(p, n) for p, n, _f in doors} >= {
        ("plugins/version_tools.py", "save_design_version"),
        ("plugins/version_tools.py", "rollback_design_version"),
        ("edit_lineage.py", "_keep"),
    }, "the doors this test knows of were not found: it is reading the wrong tree"
    unheld = [
        f"{path}::{name}" for path, name, func in doors
        if "numbers_lock" not in {getattr(n, "id", getattr(n, "attr", None)) for n in ast.walk(func)}
    ]
    assert not unheld, f"these take a version's number and never hold the design's numbers lock: {unheld}"


# ---------------------------------------------------------------------------
# An account that keeps no version numbers
# ---------------------------------------------------------------------------


class _NoNumbers(_Accounts):
    """The same accounts, answering as one that keeps no version numbers."""

    def request(self, tool, method, path, body=None, **kw):
        answer = super().request(tool, method, path, body, **kw)
        if isinstance(answer, dict):
            for key in ("account_numbers", "version_number_mark", "version_numbers"):
                answer.pop(key, None)
            answer["versions"] = [
                {k: v for k, v in row.items() if k != "version_number"} for row in answer.get("versions") or ()
            ] if "versions" in answer else answer.get("versions")
            if answer.get("versions") is None:
                answer.pop("versions", None)
        return answer


@pytest.mark.parametrize("doing", ["pull", "clone"])
def test_with_no_numbers_from_the_account_two_versions_never_share_one_here(home, monkeypatch, doing):
    """This computer and another each made version 4.  The account keeps no
    numbers.  The other's version arrives: it takes the next number free
    here, this computer's own keeps its 4, and bringing it again moves
    nothing."""
    accounts = _NoNumbers()
    monkeypatch.setattr(courier, "_request", accounts.request)
    assert courier.clone("cloud_clone_design", "vase")["status"] == "success"
    folder = _folder(home)
    mine, other = "e" * 32, "f" * 32
    (folder / ".kiln_recipe.v4.json").write_text(json.dumps({
        "name": "vase", "design_id": "vase", "version": 4, "version_id": mine,
        "source_scad": "sphere(4);", "notes": "made here", "created": "2026-11-03T00:00:00+00:00",
    }))
    accounts.designs["X"]["versions"].append((other, 4))

    def bring() -> dict:
        if doing == "pull":
            return courier.pull("cloud_pull_branch", "vase")
        return courier.clone("cloud_clone_design", "vase")

    first = bring()
    assert first["status"] == "success", first
    kept = _kept(folder)
    _numbers_agree(folder)
    assert kept[4]["version_id"] == mine, "this computer's own version was moved with no number from the account"
    assert kept[5]["version_id"] == other and len(kept) == 5, sorted(kept)
    assert "version 4 from your Kiln account is version 5" in (first.get("said_numbers") or ""), first
    assert version_numbers.next_version_number(folder) == 6

    again = bring()
    assert again["status"] == "success" and not again.get("said_numbers"), again
    assert _kept(folder) == kept, "bringing it again moved a number"
    from kiln.history_vault import HistoryVault

    vault = HistoryVault()
    try:
        assert (vault.version(other) or {}).get("recipe", {}).get("version") == 5
    finally:
        vault.close()


class _SomeNumbers(_Accounts):
    """An account whose database keeps numbers for the versions written
    since it began to, and none yet for the ones before: only the versions
    in ``numbered`` are answered with one."""

    numbered: frozenset[str] = frozenset()

    def request(self, tool, method, path, body=None, **kw):
        answer = super().request(tool, method, path, body, **kw)
        if isinstance(answer, dict):
            for key in ("account_numbers", "version_numbers"):
                if isinstance(answer.get(key), dict):
                    answer[key] = {vid: n for vid, n in answer[key].items() if vid in self.numbered}
            if "versions" in answer:
                answer["versions"] = [
                    row if row.get("version_id") in self.numbered
                    else {k: v for k, v in row.items() if k != "version_number"}
                    for row in answer["versions"]
                ]
        return answer


def test_an_account_that_numbers_only_some_versions_still_leaves_one_number_each(home, monkeypatch):
    accounts = _SomeNumbers()
    monkeypatch.setattr(courier, "_request", accounts.request)
    assert courier.clone("cloud_clone_design", "vase")["status"] == "success"
    folder = _folder(home)
    mine, unnumbered, numbered = "e" * 32, "f" * 32, "d" * 32
    (folder / ".kiln_recipe.v4.json").write_text(json.dumps({
        "name": "vase", "design_id": "vase", "version": 4, "version_id": mine,
        "source_scad": "sphere(4);", "notes": "made here", "created": "2026-11-03T00:00:00+00:00",
    }))
    accounts.designs["X"]["versions"] += [(unnumbered, 4), (numbered, 5)]
    accounts.numbered = frozenset({numbered})

    pulled = courier.pull("cloud_pull_branch", "vase")

    assert pulled["status"] == "success", pulled
    kept = _kept(folder)
    _numbers_agree(folder)
    by_id = {data["version_id"]: number for number, data in kept.items()}
    assert (by_id[mine], by_id[numbered], by_id[unnumbered]) == (4, 5, 6), sorted(by_id.items(), key=lambda kv: kv[1])
    assert "version 4 from your Kiln account is version 6" in (pulled.get("said_numbers") or ""), pulled
    again = courier.pull("cloud_pull_branch", "vase")
    assert again["status"] == "success" and _kept(folder) == kept and not again.get("said_numbers"), again


# ---------------------------------------------------------------------------
# Guards that had no test able to fail
# ---------------------------------------------------------------------------


def test_the_recipe_writer_takes_the_lock_itself_whoever_calls_it(home, monkeypatch):
    """A door that writes a version again (a rebuild, a goal noted on it)
    holds no lock of its own: the writer waits for a pull or a save that
    does, and writes nothing while it cannot."""
    folder = _folder(home)
    folder.mkdir(parents=True)
    recipe = create_recipe(name="vase", parts=[], source_scad="cube(1);")
    monkeypatch.setattr(version_numbers, "_LOCK_WAIT_SECONDS", 0.2)
    outcome: list[object] = []

    def write() -> None:
        try:
            outcome.append(save_recipe(recipe, str(folder)))
        except version_numbers.NumbersBusy as exc:
            outcome.append(exc)

    with version_numbers.numbers_lock(folder):
        thread = threading.Thread(target=write)
        thread.start()
        thread.join(10)
        assert not list(folder.glob(".kiln_recipe*.json")), "the writer wrote while another held the lock"
    assert len(outcome) == 1 and isinstance(outcome[0], version_numbers.NumbersBusy), outcome
    write()
    assert (folder / ".kiln_recipe.v1.json").is_file()


def test_when_the_files_cannot_move_the_version_store_is_put_back(home, monkeypatch):
    """The account gives this computer's version 4 another number.  The
    store's numbers are set first; when the files then fail to move, the
    store is put back and nothing here changed."""
    from kiln import design_versions
    from kiln.history_vault import HistoryVault

    accounts = _Accounts()
    monkeypatch.setattr(courier, "_request", accounts.request)
    assert courier.clone("cloud_clone_design", "vase")["status"] == "success"
    folder = _folder(home)
    store = design_versions.DesignVersionStore()
    try:
        mine = store.save_version("vase", "sphere(4);", version_number=4).version_id
    finally:
        store.close()
    (folder / ".kiln_recipe.v4.json").write_text(json.dumps({
        "name": "vase", "design_id": "vase", "version": 4, "version_id": mine,
        "source_scad": "sphere(4);", "created": "2026-11-03T00:00:00+00:00",
    }))
    before = {p.name: p.read_bytes() for p in folder.iterdir() if p.name != ".kiln_numbers.lock"}
    other = "f" * 32
    answer = {"account_numbers": {other: 4}, "version_number_mark": 4,
              "design_key": accounts.designs["X"]["key"], "design_line": accounts.designs["X"]["line"]}

    def files_fail(*_a, **_k):
        raise OSError("the disk is full")

    monkeypatch.setattr(version_numbers, "renumber_folder", files_fail)
    vault = HistoryVault()
    try:
        with pytest.raises(version_numbers.NumbersNotTaken):
            vault.take_account_numbers("vase", answer)
    finally:
        vault.close()
    store = design_versions.DesignVersionStore()
    try:
        assert store.get_version(mine).version_number == 4, "the store kept a number the files never took"
    finally:
        store.close()
    assert {p.name: p.read_bytes() for p in folder.iterdir()
            if p.name not in (".kiln_numbers.lock", ".kiln_numbers.json")} == {
        k: v for k, v in before.items() if k != ".kiln_numbers.json"}

    monkeypatch.undo()
    monkeypatch.setenv("HOME", str(home))
    vault = HistoryVault()
    try:
        taken = vault.take_account_numbers("vase", answer)
    finally:
        vault.close()
    assert taken["renumbered"] == [{"key": mine, "from": 4, "to": 5}], taken


def test_a_kept_file_that_names_no_version_moves_out_of_the_way_of_the_accounts_number(home, monkeypatch):
    """A file an older Kiln kept, naming no version, says version 4.  The
    account's version 4 arrives.  The unnamed file is a copy only this
    computer holds: it takes the next number, keeps its contents, and the
    person is told.  Two files never say one number."""
    accounts = _Accounts()
    monkeypatch.setattr(courier, "_request", accounts.request)
    assert courier.clone("cloud_clone_design", "vase")["status"] == "success"
    folder = _folder(home)
    (folder / ".kiln_recipe.v4.json").write_text(json.dumps({
        "name": "vase", "design_id": "vase", "version": 4,
        "source_scad": "sphere(4);", "notes": "kept by an older Kiln", "created": "2025-01-01T00:00:00+00:00",
    }))
    other = "f" * 32
    accounts.designs["X"]["versions"].append((other, 4))

    pulled = courier.pull("cloud_pull_branch", "vase")

    assert pulled["status"] == "success", pulled
    kept = _kept(folder)
    _numbers_agree(folder)
    assert kept[4]["version_id"] == other
    assert kept[5]["notes"] == "kept by an older Kiln" and "version_id" not in kept[5], sorted(kept)
    assert len(kept) == 5
    assert "version 4 is now version 5" in (pulled.get("said_numbers") or ""), pulled
    again = courier.pull("cloud_pull_branch", "vase")
    assert again["status"] == "success" and _kept(folder) == kept and not again.get("said_numbers"), again
