"""A printer's condition, as the person says it, and what it does at every door.

``set_machine_repair`` records two states on a machine's own record --
``needed`` (out of routing until repaired; a start still runs, with a
warning) and ``done`` (repaired; prints before it stop counting against the
machine) -- and ``clear`` takes the latest mark back.  One engine
(:mod:`kiln.machine_repairs`) behind every door, keyed by the same machine
fingerprint the print row carries.  Pinned here:

* the engine: the two-state record, what each status needs, what is
  refused by name (a status or kind off the list, a done with no kind, a
  time ahead or too far back, a machine with no stable identity, a clear
  with nothing standing, the hosted server);
* the reader: after a ``done`` at T, prints that started before T weigh 0
  in ``faded`` and leave ``machine_recent``, while the counts stay whole;
  a ``needed`` changes no number;
* the candidate builder: the ``repair`` block rides on the machine's
  registry name, status and time only, and a record that cannot be read
  attaches nothing;
* the doors: the tool, the CLI command and the start template all read
  the one engine -- the tool and the command answer in the same shape, a
  start on a printer marked needs repair runs with one warning sentence
  and is never refused;
* the wire: ``route_print_job`` sends the block to the served router and
  nothing name-shaped with it.

A/B: every test here fails at public eb4f4609 (no engine, no table, no
block, no tool, no command, no warning).
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from kiln import persistence as persistence_mod
from kiln import machine_repairs as pr
from kiln.persistence import KilnDB
from kiln.registry import machine_fingerprint

HOST, ACCESS_CODE = "192.168.7.42", "s3cret-access-code"
V1, V2 = "v-bracket-7", "v-bracket-8"
DAY = 86400.0


def _machine(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=name, serial=f"01P00A0000000{name}", host=HOST, access_code=ACCESS_CODE, _printer_model="bambu_a1",
        _kiln_registered_name=name,
        get_state=lambda: SimpleNamespace(state=SimpleNamespace(value="idle")),
    )


def _row(db: KilnDB, job: str, printer: str, outcome: str, *, version: str | None = V1,
         fingerprint: str | None = "fp", determined_by: str | None = "observed",
         started_at: float | None = None) -> None:
    """Same shape as the routing tests' helper, plus WHEN the print started
    (``created_at`` is the row's start time)."""
    row = {
        "job_id": job, "printer_name": printer, "file_name": f"{job}.gcode", "outcome": outcome,
        "determined_by": determined_by, "version_id": version, "machine_fingerprint": fingerprint,
    }
    if started_at is not None:
        row["created_at"] = started_at
    db._save_print_outcome(row)


def _counts(block: dict) -> dict:
    return {k: block[k] for k in ("completed", "failed", "partial")}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


@pytest.fixture
def db(tmp_path):
    return KilnDB(str(tmp_path / "kiln.db"))


@pytest.fixture(autouse=True)
def _own_install(monkeypatch):
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


class TestTheRecord:
    def test_needed_then_done_then_clear_is_one_record_nothing_deleted(self, db):
        a = _machine("a")
        needed = pr.mark(a, "needed", note="clicking on the X axis", db=db)
        assert needed["recorded"] is True and needed["status"] == "needed" and needed["kind"] is None
        assert needed["condition"]["state"] == "needs_repair" and needed["condition"]["since"] == needed["marked_at"]
        assert "out of routing" in needed["next"] and "still runs" in needed["next"]

        done = pr.mark(a, "done", kind="belts", db=db)
        assert done["status"] == "done" and done["kind"] == "belts" and done["reset"] == {"prints_before": 0, "versions_affected": 0}
        assert done["condition"] == {"state": "repaired", "since": None, "repaired_at": done["marked_at"], "kind": "belts", "known": True}

        cleared = pr.mark(a, "clear", db=db)
        assert cleared["status"] == "clear" and cleared["cleared"]["status"] == "done" and cleared["cleared"]["kind"] == "belts"
        assert cleared["condition"]["state"] == "needs_repair"  # the mark before it stands again

        marks = db.machine_repair_marks(machine_fingerprint(a))
        assert [m["status"] for m in marks] == ["needed", "done", "clear"]
        assert all(m["determined_by"] == "user_reported" for m in marks)
        assert marks[0]["note"] == "clicking on the X axis" and marks[0]["printer_name"] == "a"

    def test_a_cleared_record_reads_as_ok_and_a_second_clear_is_refused(self, db):
        a = _machine("a")
        pr.mark(a, "needed", db=db)
        assert pr.mark(a, "clear", db=db)["condition"]["state"] == "ok"
        with pytest.raises(pr.RepairRefused) as refused:
            pr.mark(a, "clear", db=db)
        assert refused.value.code == pr.CODE_NOTHING_TO_CLEAR

    def test_the_latest_repair_keeps_its_line_when_the_machine_is_marked_again(self, db):
        a = _machine("a")
        done = pr.mark(a, "done", kind="nozzle", db=db)
        again = pr.mark(a, "needed", db=db)
        assert again["condition"]["state"] == "needs_repair"
        assert again["condition"]["repaired_at"] == done["marked_at"]

    @pytest.mark.parametrize("kwargs, code", [
        ({"status": "fixed"}, pr.CODE_BAD_STATUS),
        ({"status": ""}, pr.CODE_BAD_STATUS),
        ({"status": "done"}, pr.CODE_KIND_REQUIRED),
        ({"status": "done", "kind": "firmware"}, pr.CODE_BAD_KIND),
        ({"status": "done", "kind": "cleaning"}, pr.CODE_BAD_KIND),
        ({"status": "needed", "kind": "wheels"}, pr.CODE_BAD_KIND),
        ({"status": "done", "kind": "bed", "when": "2099-01-01T00:00:00+00:00"}, pr.CODE_BAD_WHEN),
        ({"status": "needed", "when": "2099-01-01T00:00:00+00:00"}, pr.CODE_BAD_WHEN),
        ({"status": "done", "kind": "bed", "when": "garbage"}, pr.CODE_BAD_WHEN),
        ({"status": "done", "kind": "bed", "when": True}, pr.CODE_BAD_WHEN),
        ({"status": "needed", "note": 7}, pr.CODE_BAD_NOTE),
    ])
    def test_what_is_refused_by_name_writes_nothing(self, db, kwargs, code):
        a = _machine("a")
        with pytest.raises(pr.RepairRefused) as refused:
            pr.mark(a, db=db, **kwargs)
        assert refused.value.code == code and refused.value.message
        assert db.machine_repair_marks(machine_fingerprint(a)) == []

    def test_the_word_lists_are_the_decided_ones(self):
        assert pr.STATUSES == ("needed", "done", "clear")
        assert pr.KINDS == ("nozzle", "hotend", "extruder", "bed", "belts", "other")
        assert "firmware" not in pr.KINDS and "cleaning" not in pr.KINDS
        assert pr.BACKDATE_LIMIT_S == 30 * DAY

    def test_a_done_may_be_dated_back_a_month_and_no_further_and_never_ahead(self, db):
        a = _machine("a")
        now = time.time()
        ok = pr.mark(a, "done", kind="hotend", when=_iso(now - 29 * DAY), db=db)
        assert abs(datetime.fromisoformat(ok["marked_at"]).timestamp() - (now - 29 * DAY)) < 2
        with pytest.raises(pr.RepairRefused) as too_old:
            pr.mark(a, "done", kind="hotend", when=_iso(now - 31 * DAY), db=db)
        assert too_old.value.code == pr.CODE_BAD_WHEN and "30 days" in too_old.value.message
        with pytest.raises(pr.RepairRefused) as ahead:
            pr.mark(a, "done", kind="hotend", when=_iso(now + DAY), db=db)
        assert "future" in ahead.value.message
        # A naive time is the person's own clock; an epoch number is read too.
        naive = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
        assert abs(datetime.fromisoformat(pr.mark(a, "done", kind="bed", when=naive, db=db)["marked_at"]).timestamp() - (now - 2 * DAY)) < 120
        assert abs(datetime.fromisoformat(pr.mark(a, "done", kind="bed", when=now - DAY, db=db)["marked_at"]).timestamp() - (now - DAY)) < 2

    def test_a_machine_with_no_stable_identity_is_refused_in_a_plain_sentence(self, db):
        nobody = SimpleNamespace(name="x", get_state=lambda: None)
        with pytest.raises(pr.RepairRefused) as refused:
            pr.mark(nobody, "needed", db=db)
        assert refused.value.code == pr.CODE_NO_IDENTITY
        assert "cannot tell x apart" in refused.value.message and "serial" in refused.value.message
        assert pr.condition(nobody, db=db) == {"state": "ok", "since": None, "repaired_at": None, "kind": None, "known": False}

    def test_the_record_is_keyed_by_the_machine_the_print_row_is_keyed_by(self, db):
        a = _machine("a")
        pr.mark(a, "needed", db=db)
        assert db.machine_condition(machine_fingerprint(a))["state"] == "needs_repair"
        assert db.machine_condition(machine_fingerprint(_machine("b")))["state"] == "ok"

    def test_the_hosted_server_keeps_no_record_and_says_where_it_lives(self, db, monkeypatch):
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        with pytest.raises(pr.RepairRefused) as refused:
            pr.mark(_machine("a"), "needed", db=db)
        assert refused.value.code == pr.CODE_LOCAL_ONLY
        assert "computer your printer is connected to" in refused.value.message
        assert "tier" not in refused.value.message.lower() and "Pro" not in refused.value.message

    def test_a_done_says_how_much_of_the_record_it_sets_aside(self, db):
        a = _machine("a")
        fp = machine_fingerprint(a)
        now = time.time()
        _row(db, "p1", "a", "failed", fingerprint=fp, started_at=now - 3 * DAY)
        _row(db, "p2", "a", "failed", fingerprint=fp, started_at=now - 2 * DAY, version=V2)
        _row(db, "p3", "a", "success", fingerprint=fp, started_at=now - 2 * DAY, determined_by=None)  # nobody settled it
        _row(db, "p4", "a", "cancelled", fingerprint=fp, started_at=now - 2 * DAY)
        done = pr.mark(a, "done", kind="nozzle", when=_iso(now - DAY), db=db)
        assert done["reset"] == {"prints_before": 2, "versions_affected": 2}
        assert done["next"].startswith("2 earlier prints no longer count against it")


# ---------------------------------------------------------------------------
# The reader: a repair draws a line through the machine's own prints
# ---------------------------------------------------------------------------


class TestTheRepairResetsWhatCounts:
    def test_prints_started_before_the_repair_weigh_nothing_and_leave_the_recent_form(self, db):
        a = _machine("a")
        fp = machine_fingerprint(a)
        now = time.time()
        for i in range(3):
            _row(db, f"old{i}", "a", "failed", fingerprint=fp, started_at=now - (5 - i) * DAY)
        before = db.settled_version_results(V1)[fp]
        assert _counts(before) == {"completed": 0, "failed": 3, "partial": 0}
        assert before["faded"]["failed"] > 2.5 and before["machine_recent"] == {"completed": 0, "failed": 3, "partial": 0}

        pr.mark(a, "done", kind="nozzle", when=_iso(now - DAY), db=db)
        after = db.settled_version_results(V1)[fp]
        assert _counts(after) == {"completed": 0, "failed": 3, "partial": 0}          # the record stays whole
        assert after["faded"] == {"completed": 0.0, "failed": 0.0, "partial": 0.0}      # none of it weighs
        assert after["machine_recent"] == {"completed": 0, "failed": 0, "partial": 0}   # nothing since

        # A print started before the repair that ended after it ran on the
        # unrepaired machine; one started after counts whole.
        _row(db, "straddle", "a", "failed", fingerprint=fp, started_at=now - 1.5 * DAY)
        _row(db, "since", "a", "success", fingerprint=fp, started_at=now - 0.5 * DAY)
        since = db.settled_version_results(V1)[fp]
        assert _counts(since) == {"completed": 1, "failed": 4, "partial": 0}
        assert since["faded"] == {"completed": 1.0, "failed": 0.0, "partial": 0.0}
        assert since["machine_recent"] == {"completed": 1, "failed": 0, "partial": 0}

    def test_a_needed_mark_changes_no_number_and_a_cleared_done_counts_again(self, db):
        a = _machine("a")
        fp = machine_fingerprint(a)
        _row(db, "f1", "a", "failed", fingerprint=fp, started_at=time.time() - DAY)
        whole = db.settled_version_results(V1)[fp]
        pr.mark(a, "needed", db=db)
        assert db.settled_version_results(V1)[fp] == whole
        pr.mark(a, "done", kind="bed", db=db)
        assert db.settled_version_results(V1)[fp]["faded"]["failed"] == 0.0
        pr.mark(a, "clear", db=db)
        assert db.settled_version_results(V1)[fp] == whole

    def test_the_latest_of_two_repairs_draws_the_line(self, db):
        a = _machine("a")
        fp = machine_fingerprint(a)
        now = time.time()
        _row(db, "f1", "a", "failed", fingerprint=fp, started_at=now - 10 * DAY)
        _row(db, "f2", "a", "failed", fingerprint=fp, started_at=now - 4 * DAY)
        pr.mark(a, "done", kind="nozzle", when=_iso(now - 7 * DAY), db=db)
        assert db.settled_version_results(V1)[fp]["faded"]["failed"] == pytest.approx(1.0)
        pr.mark(a, "done", kind="hotend", when=_iso(now - 2 * DAY), db=db)
        assert db.settled_version_results(V1)[fp]["faded"]["failed"] == 0.0

    def test_another_machines_repair_touches_nothing_here(self, db):
        a, b = _machine("a"), _machine("b")
        _row(db, "f1", "a", "failed", fingerprint=machine_fingerprint(a), started_at=time.time() - DAY)
        pr.mark(b, "done", kind="nozzle", db=db)
        assert db.settled_version_results(V1)[machine_fingerprint(a)]["faded"]["failed"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# The candidate builder
# ---------------------------------------------------------------------------


class _Store:
    def __init__(self, conditions: dict[str, dict], *, raise_on_read: bool = False):
        self.conditions = conditions
        self.raise_on_read = raise_on_read

    def settled_version_results(self, version_id: str):
        return {}

    def get_printer_learning_insights(self, name: str):
        return {"success_rate": None}

    def machine_condition(self, fingerprint: str):
        if self.raise_on_read:
            raise RuntimeError("store locked")
        return self.conditions.get(fingerprint) or {"state": "ok", "since": None, "repaired_at": None, "kind": None, "marks": []}


class TestCandidatesCarryTheCondition:
    def _collect(self, monkeypatch, store, machines):
        from kiln.routing_candidates import collect_routing_candidates

        monkeypatch.setattr(persistence_mod, "get_db", lambda: store)
        return {c["printer_id"]: c for c in collect_routing_candidates(adapters=machines, material="PLA")}

    def test_the_block_rides_on_the_name_with_status_and_time_only(self, monkeypatch):
        a, b, c = _machine("a"), _machine("b"), _machine("c")
        t = time.time() - DAY
        store = _Store({
            machine_fingerprint(a): {"state": "needs_repair", "since": t, "repaired_at": None, "kind": None, "marks": []},
            machine_fingerprint(b): {"state": "repaired", "since": None, "repaired_at": t, "kind": "nozzle", "marks": [{"note": "secret place"}]},
        })
        rows = self._collect(monkeypatch, store, {"a": a, "b": b, "c": c})
        assert rows["a"]["repair"] == {"status": "needed", "since": _iso(t)}
        assert rows["b"]["repair"] == {"status": "done", "at": _iso(t), "kind": "nozzle"}
        assert "repair" not in rows["c"]
        wire = json.dumps(list(rows.values()))
        assert "secret place" not in wire and machine_fingerprint(a) not in wire and HOST not in wire

    def test_a_record_that_cannot_be_read_attaches_nothing(self, monkeypatch):
        a = _machine("a")
        rows = self._collect(monkeypatch, _Store({}, raise_on_read=True), {"a": a})
        assert "repair" not in rows["a"] and rows["a"]["printer_id"] == "a"

    def test_a_machine_with_no_stable_identity_gets_no_block(self, monkeypatch):
        nobody = SimpleNamespace(name="x", get_state=lambda: SimpleNamespace(state=SimpleNamespace(value="idle")))
        store = _Store({"x:object:deadbeef": {"state": "needs_repair", "since": 1.0, "repaired_at": None, "kind": None, "marks": []}})
        assert "repair" not in self._collect(monkeypatch, store, {"x": nobody})["x"]

    def test_the_real_store_feeds_the_real_builder(self, monkeypatch, db):
        a = _machine("a")
        pr.mark(a, "needed", db=db)
        rows = self._collect(monkeypatch, db, {"a": a})
        assert rows["a"]["repair"]["status"] == "needed"


# ---------------------------------------------------------------------------
# The doors: tool, command, start
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
        if name not in self.adapters:
            from kiln.registry import PrinterNotFoundError

            raise PrinterNotFoundError(name)
        return self.adapters[name]


@pytest.fixture
def doors(monkeypatch, db):
    """The real tool and the real command, over a registry of two machines
    and this install's own store."""
    import kiln.server as srv
    from kiln.plugins import printer_management_tools

    a, b = _machine("a"), _machine("b")
    registry = _Registry({"a": a, "b": b})
    monkeypatch.setattr(persistence_mod, "get_db", lambda: db)
    monkeypatch.setattr(srv, "_check_auth", lambda scope: None)
    monkeypatch.setattr(srv, "_get_registry", lambda: registry)
    monkeypatch.setattr(srv, "ensure_runtime_config", lambda: None)
    mcp = _FakeMCP()
    printer_management_tools.plugin.register(mcp)
    return mcp.tools["set_machine_repair"], a, b


class TestTheToolDoor:
    def test_the_tool_answers_in_the_decided_shape(self, doors, db):
        tool, a, _ = doors
        fp = machine_fingerprint(a)
        now = time.time()
        _row(db, "f1", "a", "failed", fingerprint=fp, started_at=now - 2 * DAY)
        answer = tool("a", "done", kind="nozzle", note="swapped the 0.4", when=_iso(now - DAY))
        assert answer["success"] is True and answer["recorded"] is True
        assert answer["printer"] == "a" and answer["status"] == "done" and answer["kind"] == "nozzle"
        assert abs(datetime.fromisoformat(answer["marked_at"]).timestamp() - (now - DAY)) < 2
        assert answer["reset"] == {"prints_before": 1, "versions_affected": 1}
        assert answer["condition"]["state"] == "repaired"
        assert answer["next"] == "1 earlier print no longer counts against it; its next prints decide its standing."
        for word in ("tier", "Pro", "Business", "upgrade"):
            assert word not in json.dumps(answer)
        assert db.machine_condition(fp)["state"] == "repaired"

    def test_the_tool_refuses_by_name_and_writes_nothing(self, doors, db):
        tool, a, _ = doors
        assert tool("a", "done")["error"]["code"] == pr.CODE_KIND_REQUIRED
        assert tool("a", "done", kind="firmware")["error"]["code"] == pr.CODE_BAD_KIND
        assert tool("a", "needed", when="2099-01-01")["error"]["code"] == pr.CODE_BAD_WHEN
        missing = tool("nobody", "needed")
        assert missing["error"]["code"] == pr.CODE_NOT_FOUND and "nobody" in missing["error"]["message"]
        assert db.machine_repair_marks(machine_fingerprint(a)) == []

    def test_the_tool_is_refused_on_the_hosted_server_in_plain_words(self, doors, monkeypatch):
        tool, _, _ = doors
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        answer = tool("a", "needed")
        assert answer["error"]["code"] == pr.CODE_LOCAL_ONLY
        assert "computer your printer is connected to" in answer["error"]["message"]

    def test_the_docstring_says_the_backdating_window_the_engine_enforces(self, doors):
        tool, _, _ = doors
        assert f"{pr.BACKDATE_LIMIT_S // 86400} days" in tool.__doc__


class TestTheCommandDoor:
    def _run(self, args: list[str]):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        return CliRunner().invoke(cli, ["machine", "repair", *args])

    def test_the_command_runs_the_same_engine_and_answers_the_same_shape(self, doors, db):
        _, a, _ = doors
        result = self._run(["a", "needed", "--note", "belt slipping", "--json"])
        assert result.exit_code == 0, result.output
        body = json.loads(result.output[result.output.index("{"):])
        assert body["status"] == "success" and body["data"]["status"] == "needed" and body["data"]["printer"] == "a"
        assert body["data"]["condition"]["state"] == "needs_repair"
        assert db.machine_repair_marks(machine_fingerprint(a))[0]["note"] == "belt slipping"
        done = self._run(["a", "done", "--kind", "belts", "--json"])
        assert done.exit_code == 0, done.output
        assert json.loads(done.output[done.output.index("{"):])["data"]["reset"] == {"prints_before": 0, "versions_affected": 0}

    def test_the_command_refuses_with_the_engines_own_words(self, doors):
        result = self._run(["a", "done", "--json"])
        assert result.exit_code == 1
        assert pr.CODE_KIND_REQUIRED in result.output
        off_list = self._run(["a", "done", "--kind", "firmware"])
        assert off_list.exit_code != 0 and "firmware" in off_list.output

    def test_the_command_is_a_machine_command(self):
        from kiln.cli.main import cli

        assert "repair" in cli.commands["machine"].commands


class TestTheStartDoor:
    @pytest.fixture
    def printer(self, monkeypatch, db, tmp_path):
        from kiln import preview_evidence, print_consent, print_signoff, server
        from kiln.printers.base import (
            JobProgress,
            PrintResult,
            PrinterAdapter,
            PrinterCapabilities,
            PrinterFile,
            PrinterState,
            PrinterStatus,
            UploadResult,
        )

        class _Printer(PrinterAdapter):
            def __init__(self) -> None:
                self.serial = "SERIAL-A"
                self.host = ""
                self._kiln_registered_name = "a1"

            @property
            def name(self) -> str:
                return "fake"

            @property
            def capabilities(self) -> PrinterCapabilities:
                return PrinterCapabilities()

            def get_state(self) -> PrinterState:
                return PrinterState(state=PrinterStatus.IDLE, connected=True, state_age_seconds=3600.0)

            def get_job(self) -> JobProgress:
                return JobProgress()

            def list_files(self) -> list[PrinterFile]:
                return [PrinterFile(name="part.gcode", path="part.gcode", size_bytes=1)]

            def upload_file(self, file_path: str) -> UploadResult:
                return UploadResult(success=True, file_name=os.path.basename(file_path), message="ok")

            def _start_print_impl(self, file_name: str, **kwargs) -> PrintResult:
                return PrintResult(success=True, message="started")

            def cancel_print(self) -> PrintResult:
                return PrintResult(success=True, message="")

            def pause_print(self) -> PrintResult:
                return PrintResult(success=True, message="")

            def _resume_print_impl(self) -> PrintResult:
                return PrintResult(success=True, message="")

            def emergency_stop(self) -> PrintResult:
                return PrintResult(success=True, message="")

            def _load_filament_impl(self, plan):
                raise NotImplementedError

            def _unload_filament_impl(self, plan):
                raise NotImplementedError

            def _purge_filament_impl(self, plan):
                raise NotImplementedError

            def set_tool_temp(self, target: float) -> bool:
                return True

            def set_bed_temp(self, target: float) -> bool:
                return True

            def send_gcode(self, commands: list[str]) -> bool:
                return True

            def delete_file(self, file_path: str) -> bool:
                return True

        monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("KILN_EMERGENCY_PERSIST", "0")
        monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")
        monkeypatch.setattr(persistence_mod, "get_db", lambda: db)
        monkeypatch.setattr(server, "_read_config_printers", lambda: {})
        preview_evidence._reset_for_tests()
        print_signoff._reset_for_tests()
        print_consent._reset_for_tests()
        yield _Printer()
        preview_evidence._reset_for_tests()
        print_signoff._reset_for_tests()
        print_consent._reset_for_tests()

    def test_a_start_on_a_printer_marked_needs_repair_runs_with_one_warning_and_is_never_refused(self, printer, db):
        marked = pr.mark(printer, "needed", db=db)
        assert marked["condition"]["state"] == "needs_repair"
        result = printer.start_print("part.gcode")
        assert result.success is True and not result.refused_before_send
        assert result.message.startswith("started ")
        warning = result.message[len("started "):]
        assert warning.startswith("a1 is marked needs repair since ")
        assert "started as asked" in warning and "set_machine_repair" in warning
        assert warning.count(". ") == 1  # one sentence of warning, one of what to do next

    def test_a_start_on_a_printer_in_good_standing_says_nothing_about_repairs(self, printer, db):
        assert printer.start_print("part.gcode").message == "started"
        pr.mark(printer, "done", kind="nozzle", db=db)
        assert printer.start_print("part.gcode").message == "started"

    def test_the_warning_reads_the_one_engine(self, printer, db):
        assert pr.start_warning(printer, db=db) is None
        pr.mark(printer, "needed", db=db)
        assert pr.start_warning(printer, db=db).startswith("a1 is marked needs repair since")
        pr.mark(printer, "done", kind="other", db=db)
        assert pr.start_warning(printer, db=db) is None


# ---------------------------------------------------------------------------
# The wire: what route_print_job sends
# ---------------------------------------------------------------------------


PLAN = {"success": True, "routing": {"recommended_printer": {"printer_id": "b", "plate": {"status": "clear"}}, "alternatives": [], "excluded": []}}


def _cube(path) -> str:
    import struct

    size = 20.0
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


class TestTheWire:
    def test_route_print_job_sends_the_block_and_nothing_name_shaped(self, monkeypatch, tmp_path, db):
        monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
        import kiln.server as srv
        from kiln.plugins import fleet_tools
        from kiln.queue import PrintQueue

        a, b = _machine("a"), _machine("b")
        registry = _Registry({"a": a, "b": b})
        monkeypatch.setattr(persistence_mod, "get_db", lambda: db)
        monkeypatch.setattr(srv, "_check_auth", lambda scope: None)
        monkeypatch.setattr(srv, "requires_tier", lambda tier: (lambda fn: fn), raising=False)
        monkeypatch.setattr(srv, "_get_queue", lambda: PrintQueue())
        monkeypatch.setattr(srv, "_get_registry", lambda: registry)
        monkeypatch.setattr(srv, "_registry", registry, raising=False)
        monkeypatch.setitem(sys.modules, "kiln.job_router", None)
        monkeypatch.setitem(sys.modules, "kiln.placement_fleet", None)
        sent: list[dict] = []

        def served(tool, _timeout=None, _asked_by_user=True, **kw):
            sent.append({"tool": tool, **kw})
            return PLAN

        monkeypatch.setattr(srv, "_pro_api_call", served)
        mcp = _FakeMCP()
        fleet_tools._FleetToolsPlugin().register(mcp)
        door = mcp.tools["route_print_job"]

        marked = pr.mark(a, "needed", note="at the back of the shop", db=db)
        assert door(_cube(tmp_path / "cube.stl"), material="PLA") == PLAN
        by_name = {c["printer_id"]: c for c in sent[0]["candidates"]}
        assert by_name["a"]["repair"] == {"status": "needed", "since": marked["marked_at"]}
        assert "repair" not in by_name["b"]
        wire = json.dumps(sent[0])
        assert "back of the shop" not in wire and machine_fingerprint(a) not in wire and HOST not in wire


# --- defects a lap found (2026-10-25; each fails at 3ce7748a) ---


class TestTheFleetView:
    """The repair record's design (2026-10-25, Adam: one tool, two states)
    named ``fleet_status`` among the places that say "needs repair since":
    the twenty-machine view is where a person looks for which printers are
    out of service.  The branch wired routing, the start warning and the web
    grid, and left fleet_status saying nothing -- so a printer taken out of
    service read as idle and free for work in the one listing a fleet owner
    reads first."""

    @pytest.fixture
    def fleet(self, monkeypatch, db):
        from unittest.mock import MagicMock

        import kiln.server as srv
        from kiln.printers.base import PrinterState, PrinterStatus
        from kiln.printers.octoprint import OctoPrintAdapter
        from kiln.registry import PrinterRegistry

        monkeypatch.setattr("kiln.licensing.check_tier", lambda _tier: (True, None))
        monkeypatch.setattr("kiln.account_plan._covers", lambda _required: True)
        monkeypatch.setattr(persistence_mod, "get_db", lambda: db)
        registry = PrinterRegistry()
        for name, host in (("a", "10.0.0.5"), ("b", "10.0.0.6")):
            adapter = MagicMock(spec=OctoPrintAdapter)
            adapter.name, adapter.host, adapter.serial = "OctoPrint", host, None
            adapter.get_state.return_value = PrinterState(
                connected=True, state=PrinterStatus.IDLE, tool_temp_actual=24.0, tool_temp_target=0.0,
                bed_temp_actual=22.0, bed_temp_target=0.0,
            )
            registry.register(name, adapter)
        monkeypatch.setattr(srv, "_registry", registry)
        return srv.fleet_status

    def test_a_printer_marked_needs_repair_is_listed_with_its_mark(self, fleet, db):
        pr.mark("a", "needed", db=db)
        pr.mark("b", "done", kind="belts", db=db)
        out = fleet()
        assert out["success"] is True
        assert out["needs_repair_printers"] == ["a"]
        rows = {row["name"]: row for row in out["printers"]}
        assert rows["a"]["repair"]["status"] == "needed" and rows["a"]["repair"]["since"]
        assert rows["b"]["repair"] == {"status": "done", "at": rows["b"]["repair"]["at"], "kind": "belts"}
        # Status and time only: never the note, never a code.
        assert set(rows["a"]["repair"]) == {"status", "since"}

    def test_a_fleet_with_nothing_on_record_says_nothing(self, fleet):
        out = fleet()
        assert out["needs_repair_printers"] == []
        assert all("repair" not in row for row in out["printers"])
