"""Routing a print of a kept version scores each machine by its own settled results with it.

Until this landed the fleet router was never told which design version it
was routing: a candidate's whole-printer success rate rode along and the
version's own history on each machine -- the sharpest signal there is for
the job -- stayed in the print rows, unread.  Now:

* the one reader (``KilnDB.settled_version_results``) counts, per machine
  fingerprint, the prints of a version that ENDED one of the three ways and
  that something settled (``determined_by``) -- a pending, unknown or
  cancelled row, a decided word nobody stands behind, and a row that names
  no machine never count;
* the candidate builder every routing door calls attaches those counts to
  the matching registry name as ``version_results`` and sends no fingerprint;
* ``version_of_print_file`` resolves the version from the file the way the
  print's own row does, so a route and the print it leads to agree on what
  was printed;
* ``route_print_job`` passes the version -- named by the caller or resolved
  from the file -- into the candidates and onto the served wire, and sends
  nothing version-shaped when there is none, so an older server reads the
  request exactly as before.

A/B: every test here fails at public 3056c991 (no reader, no block, no
resolver, no ``version_id`` on the door).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kiln.persistence import FADE_HALF_LIFE_PRINTS, RECENT_PRINTS, KilnDB
from kiln.registry import machine_fingerprint

HOST, ACCESS_CODE = "192.168.7.42", "s3cret-access-code"
V1, V2 = "v-bracket-7", "v-bracket-8"


def _machine(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=name, serial=f"01P00A0000000{name}", host=HOST, access_code=ACCESS_CODE, _printer_model="bambu_a1",
        get_state=lambda: SimpleNamespace(state=SimpleNamespace(value="idle")),
    )


def _counts(block: dict) -> dict:
    return {k: block[k] for k in ("completed", "failed", "partial")}


def _row(db: KilnDB, job: str, printer: str, outcome: str, *, version: str | None = V1,
         fingerprint: str | None = "fp", determined_by: str | None = "observed") -> None:
    db._save_print_outcome({
        "job_id": job, "printer_name": printer, "file_name": f"{job}.gcode", "outcome": outcome,
        "determined_by": determined_by, "version_id": version, "machine_fingerprint": fingerprint,
    })


# ---------------------------------------------------------------------------
# The reader: settled endings only, per machine
# ---------------------------------------------------------------------------


class TestSettledResultsOffThePrintRow:
    @pytest.fixture
    def db(self, tmp_path):
        return KilnDB(str(tmp_path / "kiln.db"))

    def test_counts_only_settled_endings_per_machine(self, db):
        _row(db, "a1", "shop-a", "success", fingerprint="fp-a")
        _row(db, "a2", "shop-a", "failed", fingerprint="fp-a", determined_by="user_reported")
        _row(db, "a3", "shop-a", "partial", fingerprint="fp-a", determined_by="seen")
        _row(db, "a4", "shop-a", "success", fingerprint="fp-a", determined_by=None)   # nobody settled it
        _row(db, "a5", "shop-a", "success", fingerprint="fp-a", version=V2)           # another version
        _row(db, "a6", "shop-a", "cancelled", fingerprint="fp-a")
        _row(db, "b1", "shop-b", "failed", fingerprint="fp-b")
        _row(db, "b2", "shop-b", "failed", fingerprint="fp-b")
        _row(db, "n1", "shop-n", "success", fingerprint=None)                         # names no machine
        # Open endings, written last so no decided row above resolves them.
        _row(db, "a7", "shop-a", "pending", fingerprint="fp-a", determined_by=None)
        _row(db, "a8", "shop-a", "unknown", fingerprint="fp-a")

        results = db.settled_version_results(V1)
        assert set(results) == {"fp-a", "fp-b"}
        assert _counts(results["fp-a"]) == {"completed": 1, "failed": 1, "partial": 1}
        assert _counts(results["fp-b"]) == {"completed": 0, "failed": 2, "partial": 0}
        only_b = db.settled_version_results(V1, machine_fingerprint="fp-b")
        assert set(only_b) == {"fp-b"} and _counts(only_b["fp-b"]) == {"completed": 0, "failed": 2, "partial": 0}
        assert _counts(db.settled_version_results(V2)["fp-a"]) == {"completed": 1, "failed": 0, "partial": 0}

    def test_a_result_fades_as_the_machine_prints_more_and_a_rested_machine_keeps_its_history(self, db):
        """Age is measured in the machine's own prints, never in days: a
        failure followed by 20 more prints of anything on that machine weighs
        half; the same failure on a machine that has not printed since
        weighs whole, however long ago it was."""
        _row(db, "busy-fail", "busy", "failed", fingerprint="fp-busy")
        for i in range(FADE_HALF_LIFE_PRINTS):
            _row(db, f"busy-later-{i}", "busy", "success", fingerprint="fp-busy", version=V2)
        _row(db, "rested-fail", "rested", "failed", fingerprint="fp-rested")
        _row(db, "other-machine", "elsewhere", "success", fingerprint="fp-elsewhere", version=V2)

        results = db.settled_version_results(V1)
        assert results["fp-busy"]["faded"]["failed"] == pytest.approx(0.5)
        assert results["fp-rested"]["faded"]["failed"] == pytest.approx(1.0)
        assert _counts(results["fp-busy"]) == {"completed": 0, "failed": 1, "partial": 0}  # the count itself never fades

    def test_an_unsettled_or_cancelled_print_does_not_age_a_result(self, db):
        _row(db, "f", "m", "failed", fingerprint="fp")
        _row(db, "c", "m", "cancelled", fingerprint="fp", version=V2)
        _row(db, "u", "m", "unknown", fingerprint="fp", version=V2)
        _row(db, "p", "m", "pending", fingerprint="fp", version=V2, determined_by=None)
        assert db.settled_version_results(V1)["fp"]["faded"]["failed"] == pytest.approx(1.0)

    def test_the_machines_recent_prints_of_anything_ride_beside_the_versions(self, db):
        """The last RECENT_PRINTS settled prints of the machine, whatever
        version, so a machine failing everything is read as a machine
        problem and not blamed on the version."""
        _row(db, "v1", "m", "success", fingerprint="fp")
        for i in range(RECENT_PRINTS + 2):
            _row(db, f"any-{i}", "m", "failed", fingerprint="fp", version=V2)
        _row(db, "open", "m", "unknown", fingerprint="fp", version=V2)
        recent = db.settled_version_results(V1)["fp"]["machine_recent"]
        assert recent == {"completed": 0, "failed": RECENT_PRINTS, "partial": 0}

    def test_a_version_never_printed_here_and_no_version_read_as_nothing(self, db):
        _row(db, "a1", "shop-a", "success", fingerprint="fp-a")
        assert db.settled_version_results("never-printed") == {}
        assert db.settled_version_results("") == {}
        assert db.settled_version_results(None) == {}  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The candidate builder: counts under the registry name, never a fingerprint
# ---------------------------------------------------------------------------


class _Store:
    def __init__(self, by_machine: dict[str, dict[str, int]], *, raise_on_read: bool = False):
        self.by_machine = by_machine
        self.raise_on_read = raise_on_read
        self.asked: list[str] = []

    def settled_version_results(self, version_id: str):
        self.asked.append(version_id)
        if self.raise_on_read:
            raise RuntimeError("store locked")
        return dict(self.by_machine)

    def get_printer_learning_insights(self, name: str):
        return {"success_rate": None}


class TestCandidatesCarryTheMachinesOwnResults:
    def _collect(self, monkeypatch, store, machines, **kw):
        import kiln.persistence as persistence
        from kiln.routing_candidates import collect_routing_candidates

        monkeypatch.setattr(persistence, "get_db", lambda: store)
        return collect_routing_candidates(adapters=machines, material="PLA", **kw)

    def test_each_machines_counts_land_on_its_name_and_the_fingerprint_stays_home(self, monkeypatch):
        a, b = _machine("a"), _machine("b")
        store = _Store({machine_fingerprint(a): {"completed": 2, "failed": 1, "partial": 0}})
        rows = {c["printer_id"]: c for c in self._collect(monkeypatch, store, {"a": a, "b": b}, version_id=V1)}
        assert store.asked == [V1]
        assert rows["a"]["version_results"] == {"completed": 2, "failed": 1, "partial": 0}
        assert "version_results" not in rows["b"]
        wire = json.dumps(list(rows.values()))
        assert machine_fingerprint(a) not in wire and a.serial.lower() not in wire and HOST not in wire

    def test_no_version_asks_nothing_and_attaches_nothing(self, monkeypatch):
        a = _machine("a")
        store = _Store({machine_fingerprint(a): {"completed": 2, "failed": 0, "partial": 0}})
        rows = self._collect(monkeypatch, store, {"a": a})
        assert store.asked == [] and "version_results" not in rows[0]

    def test_a_read_that_fails_leaves_the_fleet_whole_with_no_history(self, monkeypatch):
        a = _machine("a")
        rows = self._collect(monkeypatch, _Store({}, raise_on_read=True), {"a": a}, version_id=V1)
        assert [c["printer_id"] for c in rows] == ["a"] and "version_results" not in rows[0]

    def test_a_machine_with_no_stable_identity_gets_no_block(self, monkeypatch):
        nobody = SimpleNamespace(name="x", get_state=lambda: SimpleNamespace(state=SimpleNamespace(value="idle")))
        store = _Store({"x:object:deadbeef": {"completed": 5, "failed": 0, "partial": 0}})
        rows = self._collect(monkeypatch, store, {"x": nobody}, version_id=V1)
        assert "version_results" not in rows[0]


# ---------------------------------------------------------------------------
# Which version a file is a print of
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KILN_DB_PATH", str(tmp_path / "kiln.db"))
    monkeypatch.setenv("HOME", str(tmp_path))
    import kiln.history_vault as hv
    import kiln.monitor_twin as mt

    monkeypatch.setattr(hv, "_kiln_home", lambda: tmp_path / ".kiln")
    twin = tmp_path / ".kiln" / "monitor_twin"
    twin.mkdir(parents=True)
    monkeypatch.setattr(mt, "_TWIN_DIR", twin)
    monkeypatch.setattr(mt, "_SLICES_FILE", twin / "slices.json")
    return tmp_path


def _keep_version(home: Path, version_id: str, design_id: str, data: bytes) -> None:
    from kiln.history_vault import HistoryVault

    HistoryVault().close()
    conn = sqlite3.connect(str(home / ".kiln" / "history_vault.db"))
    conn.execute(
        "INSERT INTO versions (version_id, design_id, parent_version_id, created_at, recipe, mesh_sha256) "
        "VALUES (?, ?, NULL, ?, '{}', ?)",
        (version_id, design_id, time.time(), hashlib.sha256(data).hexdigest()),
    )
    conn.commit()
    conn.close()


MODEL = b"solid bracket v7 bytes"


class TestTheVersionOfAPrintFile:
    def test_a_kept_models_own_bytes_name_its_version(self, home):
        from kiln.auto_record_hook import version_of_print_file

        _keep_version(home, V1, "bracket", MODEL)
        model = home / "bracket.stl"
        model.write_bytes(MODEL)
        assert version_of_print_file(str(model)) == V1
        other = home / "other.stl"
        other.write_bytes(b"some other part")
        assert version_of_print_file(str(other)) is None

    def test_a_file_this_computer_sliced_names_the_version_its_model_is(self, home):
        from kiln.auto_record_hook import version_of_print_file
        from kiln.monitor_twin import note_sliced

        _keep_version(home, V1, "bracket", MODEL)
        model, gcode = home / "bracket.stl", home / "bracket.gcode"
        model.write_bytes(MODEL)
        gcode.write_text("; gcode")
        note_sliced(str(model), str(gcode))
        assert version_of_print_file(str(gcode)) == V1

    def test_two_versions_of_one_model_name_no_version(self, home):
        from kiln.auto_record_hook import version_of_print_file

        _keep_version(home, V1, "bracket", MODEL)
        _keep_version(home, V2, "bracket", MODEL)
        model = home / "bracket.stl"
        model.write_bytes(MODEL)
        assert version_of_print_file(str(model)) is None
        assert version_of_print_file(None) is None and version_of_print_file("") is None


# ---------------------------------------------------------------------------
# The door: route_print_job on a plain install, what rides the wire
# ---------------------------------------------------------------------------


class _FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class _Registry:
    def __init__(self, machines):
        self.adapters = machines

    def list_names(self):
        return list(self.adapters)

    def get(self, name):
        return self.adapters[name]


def _cube(path: Path, size: float = 20.0) -> str:
    v = [(0, 0, 0), (size, 0, 0), (size, size, 0), (0, size, 0), (0, 0, size), (size, 0, size), (size, size, size), (0, size, size)]
    faces = [(0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80)
        fh.write(struct.pack("<I", len(faces)))
        for a, b, c in faces:
            fh.write(struct.pack("<3f", 0, 0, 0))
            for i in (a, b, c):
                fh.write(struct.pack("<3f", *v[i]))
            fh.write(struct.pack("<H", 0))
    return str(path)


PLAN = {"success": True, "routing": {"recommended_printer": {"printer_id": "a", "plate": {"status": "clear"}}, "alternatives": [], "excluded": []}}


@pytest.fixture
def plain_install(monkeypatch, tmp_path):
    """The real door with kiln-pro absent, the served call stood in, and the
    print rows this install keeps."""
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
    import kiln.persistence as persistence
    import kiln.server as srv
    from kiln.plugins import fleet_tools
    from kiln.queue import PrintQueue

    store = KilnDB(str(tmp_path / "kiln.db"))
    monkeypatch.setattr(persistence, "get_db", lambda: store)
    monkeypatch.setattr(srv, "_check_auth", lambda scope: None)
    monkeypatch.setattr(srv, "requires_tier", lambda tier: (lambda fn: fn), raising=False)
    monkeypatch.setattr(srv, "_get_queue", lambda: PrintQueue())
    monkeypatch.setitem(sys.modules, "kiln.job_router", None)
    monkeypatch.setitem(sys.modules, "kiln.placement_fleet", None)

    def make(machines: dict[str, Any]):
        registry = _Registry(machines)
        monkeypatch.setattr(srv, "_get_registry", lambda: registry)
        monkeypatch.setattr(srv, "_registry", registry, raising=False)
        sent: list[dict] = []

        def served(tool, _timeout=None, _asked_by_user=True, **kw):
            sent.append({"tool": tool, **kw})
            return PLAN

        monkeypatch.setattr(srv, "_pro_api_call", served)
        mcp = _FakeMCP()
        fleet_tools._FleetToolsPlugin().register(mcp)
        return mcp.tools["route_print_job"], sent, store

    return make


class TestRoutePrintJobPassesTheVersion:
    def test_the_named_version_rides_with_each_machines_counts_and_no_fingerprint(self, plain_install, tmp_path):
        a, b = _machine("a"), _machine("b")
        door, sent, store = plain_install({"a": a, "b": b})
        _row(store, "j1", "a", "success", fingerprint=machine_fingerprint(a))
        _row(store, "j2", "a", "failed", fingerprint=machine_fingerprint(a))
        _row(store, "j3", "b", "success", fingerprint=machine_fingerprint(b), version=V2)
        assert door(_cube(tmp_path / "cube.stl"), material="PLA", version_id=V1) == PLAN
        body = sent[0]
        assert body["tool"] == "plan_fleet_route" and body["version_id"] == V1
        by_name = {c["printer_id"]: c for c in body["candidates"]}
        block = by_name["a"]["version_results"]
        assert _counts(block) == {"completed": 1, "failed": 1, "partial": 0}
        assert set(block["faded"]) == {"completed", "failed", "partial"}
        assert block["machine_recent"] == {"completed": 1, "failed": 1, "partial": 0}
        assert "version_results" not in by_name["b"]
        wire = json.dumps(body)
        assert machine_fingerprint(a) not in wire and HOST not in wire and ACCESS_CODE not in wire

    def test_no_version_sends_nothing_version_shaped(self, plain_install, tmp_path):
        a = _machine("a")
        door, sent, store = plain_install({"a": a})
        _row(store, "j1", "a", "success", fingerprint=machine_fingerprint(a))
        door(_cube(tmp_path / "cube.stl"), material="PLA")
        body = sent[0]
        assert "version_id" not in body
        assert all("version_results" not in c for c in body["candidates"])

    def test_an_unnamed_version_is_resolved_from_the_file_the_way_the_print_row_resolves_it(
        self, plain_install, tmp_path, monkeypatch,
    ):
        import kiln.auto_record_hook as hook

        a = _machine("a")
        door, sent, store = plain_install({"a": a})
        _row(store, "j1", "a", "failed", fingerprint=machine_fingerprint(a), version=V2)
        path = _cube(tmp_path / "cube.stl")
        seen: list[str] = []

        def resolve(file_path):
            seen.append(file_path)
            return V2

        monkeypatch.setattr(hook, "version_of_print_file", resolve)
        door(path, material="PLA")
        assert seen == [path]
        body = sent[0]
        assert body["version_id"] == V2
        assert _counts(body["candidates"][0]["version_results"]) == {"completed": 0, "failed": 1, "partial": 0}
