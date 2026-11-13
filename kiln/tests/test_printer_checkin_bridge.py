"""The printer check-in: records up, one question down, shown once, answered once.

Driven against a fake of Kiln's servers that answers in the wire shape, through
the real request helper, this install's real store, the real repair record and
the real ``set_machine_repair`` tool.  Pinned:

* nothing leaves but the named fields, and never a file's name or
  fingerprint, a job id, a serial, a note or settings;
* each ``do`` word does exactly its thing;
* a miss of each of the four kinds asks nothing and breaks nothing;
* the ending's own question comes first, and the slot never holds two;
* an answered episode is sent with later check-ins and never shown again;
* a slow server never delays the write or the tool that reports an ending;
* a question is shown only while the ending it came back at is the printer's
  newest print;
* an unanswered question is put to the person once, and once more for each
  new failed ending on that printer; the printer's insights list it every
  time until it is answered.
"""

from __future__ import annotations

import json as _json
import re
import time
from types import SimpleNamespace
from typing import Any

import pytest

from kiln import _pro_printer_checkin_bridge as checkin
from kiln import ending_question, machine_repairs
from kiln import persistence as persistence_mod
from kiln.persistence import KilnDB
from kiln.registry import stable_machine_fingerprint

DAY = 86400.0
SERIAL = "01P00A000000042"
HOST = "192.168.7.42"
EPISODE = "ep:" + "9f0c1d2e3a4b5c6d7e8f9a0b"

#: Every field a check-in may carry, by where it sits.
BODY_FIELDS = {"machine_code", "printer_label", "prints", "repair", "fixes", "answered"}
RECORD_FIELDS = {
    "job_id", "outcome", "failure_mode", "determined_by", "created_at", "started_at", "ended_at",
    "design_id", "file_hash", "file_code", "material_type", "nozzle_diameter_mm", "spool_code",
    "settings_code", "firmware", "progress_at_end", "machine_code",
}
REPAIR_FIELDS = {"state", "since", "marked_at", "repaired_at", "kind"}


def _machine(name: str = "Shop A1") -> SimpleNamespace:
    return SimpleNamespace(
        name=name, serial=SERIAL, host=HOST, access_code="s3cret-access", _printer_model="bambu_a1",
        _kiln_registered_name=name,
        get_state=lambda: SimpleNamespace(state=SimpleNamespace(value="idle")),
    )


def _question(**changes: Any) -> dict[str, Any]:
    """A reply's question, as the wire carries one."""
    question = {
        "episode": EPISODE,
        "about": "printer",
        "seen": "3 different designs in a row came out with shifted layers on Shop A1, "
                "in the prints Kiln watched over the last 3 days.",
        "check": {"say": "Often a loose belt.", "kind": "belts"},
        "ask": "Does the printer need repair?",
        "answers": [
            {"id": "repair", "say": "It needs repair", "do": "mark_needs_repair", "kind": "belts",
             "since": time.time() - 3 * DAY},
            {"id": "neither", "say": "Neither", "do": "remember"},
        ],
    }
    question.update(changes)
    return question


class _Servers:
    """Kiln's servers, faked at the one call the request helper makes."""

    def __init__(self) -> None:
        self.question: dict[str, Any] | None = None
        self.status = 200
        self.error: BaseException | None = None
        self.delay = 0.0
        self.calls: list[dict[str, Any]] = []
        self.raw: list[str] = []

    def request(self, method: str, url: str, *, headers=None, timeout=None, json=None, **_kw):
        self.calls.append({"method": method, "url": url, "body": json, "timeout": timeout, "headers": headers})
        self.raw.append(_json.dumps(json))
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        if self.status != 200:
            body = {"status": "error", "success": False, "code": "INVALID_INPUT", "error": "no"} \
                if self.status == 400 else {"detail": "nope"}
            return SimpleNamespace(status_code=self.status, json=lambda: body, content=b"")
        answer = {"status": "success", "success": True, "question": self.question}
        return SimpleNamespace(status_code=200, json=lambda: answer, content=b"")


@pytest.fixture
def world(tmp_path, monkeypatch):
    """This install: its store, one printer set up, signed in, and the fake servers."""
    import httpx

    import kiln.server as srv
    from kiln import auth_session, outbound_guard

    monkeypatch.setenv("KILN_DB_PATH", str(tmp_path / "kiln.db"))
    monkeypatch.setattr(persistence_mod, "_db", None, raising=False)
    db = persistence_mod.get_db()
    assert persistence_mod.db_if_open() is db
    machine = _machine()
    servers = _Servers()

    class _Registry:
        def get(self, name):
            if name != "Shop A1":
                from kiln.registry import PrinterNotFoundError

                raise PrinterNotFoundError(name)
            return machine

        def list_names(self):
            return ["Shop A1"]

    monkeypatch.setattr(persistence_mod, "get_db", lambda: db)
    monkeypatch.setattr(srv, "_get_registry", lambda: _Registry())
    monkeypatch.setattr(srv, "_check_auth", lambda *_a, **_k: None)
    monkeypatch.setattr(srv, "ensure_runtime_config", lambda: None, raising=False)
    signed = SimpleNamespace(token="test-bearer", state="ok", detail="")
    monkeypatch.setattr(auth_session, "resolve_api_bearer", lambda: signed)
    monkeypatch.setattr(auth_session, "_api_base", lambda: "https://kiln.test")
    monkeypatch.setattr(httpx, "request", servers.request)
    monkeypatch.setattr(outbound_guard, "_own_printer_values", lambda: [
        ("a printer's serial number", SERIAL), ("the printer's network address", HOST),
    ])
    checkin.forget()
    world = SimpleNamespace(
        db=db, machine=machine, servers=servers, signed=signed,
        fingerprint=stable_machine_fingerprint(machine),
    )
    yield world
    checkin.forget()
    db.close()
    monkeypatch.setattr(persistence_mod, "_db", None, raising=False)


def _print(world, job: str, outcome: str = "success", mode: str | None = None, *, days_ago: float = 1.0,
           by: str | None = "observed", **more: Any) -> None:
    started = time.time() - days_ago * DAY
    world.db._save_print_outcome({
        "job_id": job, "printer_name": "Shop A1", "file_name": f"/Users/someone/private-{job}.gcode",
        "file_hash": f"filehash{job}", "outcome": outcome, "failure_mode": mode, "determined_by": by,
        "material_type": "PLA", "machine_fingerprint": world.fingerprint,
        "design_id": "dsn-" + job.replace("shift", "s").replace("good", "g"),
        "nozzle_diameter_mm": 0.4, "notes": "a private note", "note_by": "person",
        "settings": {"temp_tool": 215}, "created_at": started, "ended_at": started + 3600, **more,
    })


def _history(world) -> None:
    """Five prints that came out, then three that did not."""
    for i in range(5):
        _print(world, f"good-{i}", days_ago=20 - i)
    for i in range(3):
        _print(world, f"shift-{i}", "failed", "layer_shift", days_ago=3 - i)


def _tool():
    from kiln.plugins import printer_management_tools

    tools: dict[str, Any] = {}

    class _FakeMCP:
        def tool(self, *a, **k):
            def deco(fn):
                tools[fn.__name__] = fn
                return fn
            return deco

    printer_management_tools.plugin.register(_FakeMCP())
    return tools["set_machine_repair"]


# ---------------------------------------------------------------------------
# What leaves
# ---------------------------------------------------------------------------


class TestWhatLeaves:
    def test_only_the_named_fields_go_and_nothing_that_names_a_file_or_a_machine(self, world):
        _history(world)
        world.db._conn.execute(
            "UPDATE print_outcomes SET settings_code = ?, spool_id = ?, firmware_version = ?, progress_at_end = ? "
            "WHERE job_id = 'shift-2'", ("ab" * 16, "spool-raw-id-77", "01.07.00.00", 0.31),
        )
        world.db._conn.commit()
        machine_repairs.mark("Shop A1", "needed", kind="belts", note="belt squeals at the shop on Elm St", db=world.db)
        machine_repairs.mark("Shop A1", "clear", db=world.db)

        assert checkin.ask_now("Shop A1", db=world.db) == {"asked": True, "why": "kept", "question": False}
        call = world.servers.calls[-1]
        assert call["method"] == "POST" and call["url"] == "https://kiln.test/api/cloud/machines/checkin"
        assert call["timeout"] == checkin.WAIT_S
        body = call["body"]
        assert set(body) <= BODY_FIELDS
        assert re.fullmatch(r"[a-z]{2}:[0-9a-f]{24}", body["machine_code"])
        assert body["printer_label"] == "Shop A1" and len(body["prints"]) == 8
        for record in body["prints"]:
            assert set(record) <= RECORD_FIELDS, set(record) - RECORD_FIELDS
            assert re.fullmatch(r"pj:[0-9a-f]{24}", record["job_id"])
            assert re.fullmatch(r"fc:[0-9a-f]{24}", record["file_code"])
            assert re.fullmatch(r"fh:[0-9a-f]{24}", record["file_hash"])
        newest = max(body["prints"], key=lambda r: r["created_at"])
        assert newest["settings_code"] == "ab" * 16 and newest["firmware"] == "01.07.00.00"
        assert re.fullmatch(r"sp:[0-9a-f]{24}", newest["spool_code"]) and newest["progress_at_end"] == 0.31
        assert newest["outcome"] == "failed" and newest["failure_mode"] == "layer_shift"
        sent = world.servers.raw[-1]
        for private in ("private-", ".gcode", "someone", "filehash", "shift-2", "good-0", "a private note",
                        "temp_tool", "spool-raw-id-77", SERIAL, SERIAL.lower(), HOST, world.fingerprint,
                        "s3cret-access", "Elm St", "bambu_a1"):
            assert private not in sent, private

    def test_one_file_gets_one_code_and_another_file_another(self, world):
        from kiln.outbound_guard import file_code, file_hash_code, job_code, machine_code, spool_code

        assert file_code("/a/b/Part.gcode") == file_code("part.gcode") != file_code("other.gcode")
        codes = {file_code("x"), file_hash_code("x"), job_code("x"), machine_code("x"), spool_code("x")}
        assert len(codes) == 5 and None not in codes
        assert file_code("") is None and file_hash_code(None) is None and job_code(" ") is None

    def test_the_repair_mark_and_the_answered_episodes_ride_along(self, world):
        _history(world)
        marked = time.time() - 2 * DAY
        machine_repairs.mark("Shop A1", "needed", kind="belts", when=marked, db=world.db)
        world.db.set_printer_checkin(world.fingerprint, "Shop A1", _question(), asked_job_id="shift-2")
        world.db.answer_printer_checkin(world.db.get_printer_checkin(EPISODE)["id"], "neither", answered_at=1799300000.0)
        checkin.ask_now("Shop A1", db=world.db)
        body = world.servers.calls[-1]["body"]
        assert set(body["repair"]) <= REPAIR_FIELDS
        assert body["repair"]["state"] == "needs_repair" and body["repair"]["kind"] == "belts"
        assert abs(body["repair"]["since"] - marked) < 2 and body["repair"]["marked_at"] > marked + DAY
        assert body["answered"] == [{"key": EPISODE, "at": 1799300000.0}]

    def test_a_name_that_holds_a_serial_is_not_sent(self, world):
        assert checkin._label(f"A1 {SERIAL}") is None and checkin._label("  Shop   A1 ") == "Shop A1"
        assert len(checkin._label("x" * 90)) == checkin.LABEL_MAX

    def test_a_print_with_no_verdict_is_not_sent(self, world):
        _history(world)
        _print(world, "stopped-by-hand", "cancelled", days_ago=0.5)
        world.db.mark_ending_asked("stopped-by-hand")
        _print(world, "nobody-saw", "unknown", days_ago=0.4)
        checkin.ask_now("Shop A1", db=world.db)
        assert len(world.servers.calls[-1]["body"]["prints"]) == 8


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


class TestWhen:
    def test_once_per_ending_as_it_stands(self, world):
        _history(world)
        assert checkin.ask_now("Shop A1", db=world.db)["why"] == "kept"
        assert checkin.ask_now("Shop A1", db=world.db) == {"asked": False, "why": "already_asked"}
        assert len(world.servers.calls) == 1
        _print(world, "next", days_ago=0.1)
        assert checkin.ask_now("Shop A1", db=world.db)["why"] == "kept" and len(world.servers.calls) == 2

    def test_never_while_the_newest_print_is_open(self, world):
        _history(world)
        world.servers.question = _question()
        checkin.ask_now("Shop A1", db=world.db)
        assert checkin.listed("Shop A1", db=world.db) is not None
        world.db._save_print_outcome({
            "job_id": "running", "printer_name": "Shop A1", "outcome": "pending",
            "machine_fingerprint": world.fingerprint,
        })
        assert checkin.ask_now("Shop A1", db=world.db) == {"asked": False, "why": "printing"}
        assert len(world.servers.calls) == 1
        assert checkin.listed("Shop A1", db=world.db) is None
        assert checkin.for_the_ending("Shop A1", db=world.db) is None

    def test_a_printer_kiln_cannot_tell_apart_and_a_signed_out_install_send_nothing(self, world):
        world.db._save_print_outcome({"job_id": "j", "printer_name": "Nameless", "outcome": "success"})
        assert checkin.ask_now("Nameless", db=world.db)["why"] == "printer_not_identified"
        _history(world)
        world.signed.token = ""
        assert checkin.ask_now("Shop A1", db=world.db) == {"asked": False, "why": "signed_out"}
        assert world.servers.calls == []

    def test_nothing_is_asked_from_kilns_own_servers(self, world, monkeypatch):
        import kiln.runtime_env as runtime_env

        _history(world)
        world.db.set_printer_checkin(world.fingerprint, "Shop A1", _question(), asked_job_id="shift-2")
        monkeypatch.setattr(runtime_env, "is_hosted_multitenant", lambda: True)
        assert checkin.ask_now("Shop A1", db=world.db) == {"asked": False, "why": "local_only"}
        assert checkin.for_the_ending("Shop A1", db=world.db) is None and world.servers.calls == []

    def test_a_settled_row_offers_the_check_in_and_a_slow_server_delays_nothing(self, world, monkeypatch):
        monkeypatch.setenv(checkin.TEST_OPT_IN_ENV, "1")
        monkeypatch.setenv("KILN_OUTCOME_COURIER", "0")
        monkeypatch.setattr(checkin, "_GATHER_S", 0.05)
        _history(world)
        world.servers.question = _question()
        world.servers.delay = 1.0

        began = time.monotonic()
        world.db.save_print_outcome({
            "job_id": "shift-3", "printer_name": "Shop A1", "outcome": "failed", "failure_mode": "layer_shift",
            "determined_by": "observed", "machine_fingerprint": world.fingerprint,
        })
        wrote = time.monotonic() - began
        time.sleep(0.3)  # the check-in is on the wire now, and the servers are slow
        began = time.monotonic()
        result = ending_question.attach({"outcome": "failed"}, "Shop A1")
        read = time.monotonic() - began
        assert wrote < 0.5 and read < 0.5, (wrote, read)
        assert len(world.servers.calls) == 1 and "ask_the_person" not in result

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and world.db.printer_checkin_waiting(world.fingerprint) is None:
            time.sleep(0.05)
        # Kept once it arrived, and carried by the next door that reports this ending.
        later = ending_question.attach({"outcome": "failed"}, "Shop A1")
        assert later["ask_the_person"]["episode"] == EPISODE

    def test_a_test_run_sends_nothing_unless_it_opts_in(self, world, monkeypatch):
        monkeypatch.delenv(checkin.TEST_OPT_IN_ENV, raising=False)
        monkeypatch.setattr(checkin, "_GATHER_S", 0.01)
        _history(world)
        world.db.save_print_outcome({
            "job_id": "x", "printer_name": "Shop A1", "outcome": "success", "machine_fingerprint": world.fingerprint,
        })
        time.sleep(0.2)
        assert world.servers.calls == []


# ---------------------------------------------------------------------------
# Misses
# ---------------------------------------------------------------------------


def _offline(servers):
    import httpx

    servers.error = httpx.ConnectError("[Errno 8] nodename nor servname provided, or not known")


def _no_answer(servers):
    servers.status = 503


def _timed_out(servers):
    import httpx

    servers.error = httpx.ReadTimeout("timed out")


def _refused(servers):
    servers.status = 400


def _too_large(servers):
    servers.status = 413


def _session_rejected(servers):
    servers.status = 401


class TestAMiss:
    @pytest.mark.parametrize(
        ("break_it", "causes"),
        [
            (_offline, {"offline", "unanswered"}),
            (_no_answer, {"unanswered"}),
            (_timed_out, {"offline", "unanswered"}),
            (_refused, {"refused"}),
            (_too_large, {"refused"}),
            (_session_rejected, {"signed_out"}),
        ],
    )
    def test_it_asks_nothing_says_nothing_and_breaks_nothing(self, world, monkeypatch, break_it, causes):
        from kiln import served_answer

        monkeypatch.setattr(served_answer, "_route_to", lambda *_a, **_k: False, raising=False)
        _history(world)
        break_it(world.servers)
        answer = checkin.ask_now("Shop A1", db=world.db)
        assert answer["asked"] is True and answer["why"] in causes and set(answer) == {"asked", "why"}
        assert world.db.printer_checkin_waiting(world.fingerprint) is None
        result = ending_question.attach({"outcome": "failed", "success": True}, "Shop A1")
        assert result == {"outcome": "failed", "success": True}
        assert checkin.listed("Shop A1", db=world.db) is None
        # The next ending asks again.
        world.servers.error, world.servers.status = None, 200
        world.servers.question = _question()
        _print(world, "shift-3", "failed", "layer_shift", days_ago=0.1)
        assert checkin.ask_now("Shop A1", db=world.db) == {"asked": True, "why": "kept", "question": True}

    def test_a_signed_out_install_asks_nothing_and_the_ending_still_carries_its_own_question(self, world):
        world.signed.token = ""
        _history(world)
        _print(world, "stop", "failed", None, days_ago=0.001, ended_at=time.time() - 30)
        assert checkin.ask_now("Shop A1", db=world.db)["asked"] is False
        asked = ending_question.attach({"outcome": "failed"}, "Shop A1")["ask_the_person"]
        assert asked["about"] == "stop"

    def test_a_miss_leaves_an_earlier_question_kept_and_unshown(self, world):
        _history(world)
        world.servers.question = _question()
        checkin.ask_now("Shop A1", db=world.db)
        _print(world, "shift-3", "failed", "layer_shift", days_ago=0.1)
        world.servers.status = 503
        assert checkin.ask_now("Shop A1", db=world.db)["why"] == "unanswered"
        # Kept, and answerable; not shown, because it came back at an earlier ending.
        assert world.db.get_printer_checkin(EPISODE)["answer"] is None
        assert checkin.for_the_ending("Shop A1", db=world.db) is None
        assert checkin.listed("Shop A1", db=world.db) is None

    def test_an_answer_that_is_not_a_question_keeps_nothing(self, world):
        _history(world)
        for bad in (
            {"episode": "not-a-key"}, _question(answers=[]), _question(seen=""),
            _question(answers=[{"id": "repair", "say": "It needs repair", "do": "reformat_the_disk"}]),
            "a string", _question(ask=None),
        ):
            world.servers.question = bad
            checkin.forget()
            assert checkin.ask_now("Shop A1", db=world.db) == {"asked": True, "why": "kept", "question": False}
            assert checkin.listed("Shop A1", db=world.db) is None


# ---------------------------------------------------------------------------
# Showing
# ---------------------------------------------------------------------------


class TestShowing:
    def test_the_block_carries_the_servers_sentences_as_written_and_a_call_per_answer(self, world):
        _history(world)
        world.servers.question = _question()
        checkin.ask_now("Shop A1", db=world.db)
        block = ending_question.attach({"outcome": "failed"}, "Shop A1")["ask_the_person"]
        sent = world.servers.question
        assert block["about"] == "printer" and block["printer"] == "Shop A1" and block["episode"] == EPISODE
        assert block["seen"] == sent["seen"] and block["question"] == sent["ask"]
        assert block["check"] == "Often a loose belt." and block["check_part"] == "belts"
        assert block["record"] == {
            "It needs repair": f'set_machine_repair(machine_name="Shop A1", answer_to="{EPISODE}", answer="repair")',
            "Neither": f'set_machine_repair(machine_name="Shop A1", answer_to="{EPISODE}", answer="neither")',
        }
        assert set(block) == {"about", "printer", "episode", "seen", "check", "check_part", "question", "record", "rule"}
        assert block["rule"] == checkin.RULE

    def test_once_per_ending_and_again_at_a_later_ending_until_answered(self, world):
        _history(world)
        world.servers.question = _question()
        checkin.ask_now("Shop A1", db=world.db)
        assert "ask_the_person" in ending_question.attach({"outcome": "failed"}, "Shop A1")
        assert "ask_the_person" not in ending_question.attach({"outcome": "failed"}, "Shop A1")
        # The same ending, read again by the servers: still shown once.
        checkin.forget()
        checkin.ask_now("Shop A1", db=world.db)
        assert "ask_the_person" not in ending_question.attach({"outcome": "failed"}, "Shop A1")
        # A later ending brings it back, once.
        _print(world, "shift-3", "failed", "layer_shift", days_ago=0.1)
        assert "ask_the_person" not in ending_question.attach({"outcome": "failed"}, "Shop A1")
        checkin.ask_now("Shop A1", db=world.db)
        assert ending_question.attach({"outcome": "failed"}, "Shop A1")["ask_the_person"]["episode"] == EPISODE
        assert "ask_the_person" not in ending_question.attach({"outcome": "failed"}, "Shop A1")

    def test_a_question_the_servers_drop_stops_waiting(self, world):
        _history(world)
        world.servers.question = _question()
        checkin.ask_now("Shop A1", db=world.db)
        _print(world, "came-out", days_ago=0.1)
        world.servers.question = None
        checkin.ask_now("Shop A1", db=world.db)
        assert world.db.printer_checkin_waiting(world.fingerprint) is None
        assert "ask_the_person" not in ending_question.attach({"outcome": "completed"}, "Shop A1", ending_can_owe=False)

    def test_the_endings_own_question_comes_first_and_the_slot_holds_one(self, world):
        _history(world)
        world.servers.question = _question()
        checkin.ask_now("Shop A1", db=world.db)
        # The printer stops the next print and names no cause.
        _print(world, "stop", "failed", None, days_ago=0.001, ended_at=time.time() - 30)
        assert checkin.ask_now("Shop A1", db=world.db) == {"asked": False, "why": "ending_question_first"}
        assert len(world.servers.calls) == 1
        result = ending_question.attach({"outcome": "failed"}, "Shop A1")
        assert result["ask_the_person"]["about"] == "stop" and "episode" not in result["ask_the_person"]
        assert checkin.for_the_ending("Shop A1", db=world.db) is None
        assert checkin.listed("Shop A1", db=world.db) is None
        # The person has nothing to add: the check-in goes out, and the slot is the printer's.
        world.db.mark_ending_asked("stop")
        assert checkin.ask_now("Shop A1", db=world.db)["why"] == "kept"
        later = ending_question.attach({"outcome": "failed"}, "Shop A1")
        assert later["ask_the_person"]["about"] == "printer"

    def test_each_wall_between_the_two_questions_holds_on_its_own(self, world, monkeypatch):
        _history(world)
        _print(world, "stop", "failed", None, days_ago=0.001, ended_at=time.time() - 30)
        # A question kept at the very ending that owes its own is still not shown.
        world.db.set_printer_checkin(world.fingerprint, "Shop A1", _question(), asked_job_id="stop")
        assert checkin.listed("Shop A1", db=world.db) is None
        assert checkin.for_the_ending("Shop A1", db=world.db) is None
        # And a reader that hands one over anyway does not take the slot.
        monkeypatch.setattr(checkin, "for_the_ending", lambda *_a, **_k: {"about": "printer"})
        assert ending_question.attach({"outcome": "failed"}, "Shop A1")["ask_the_person"]["about"] == "stop"

    def test_a_print_reopened_under_the_same_job_shows_nothing(self, world):
        _history(world)
        world.servers.question = _question()
        checkin.ask_now("Shop A1", db=world.db)
        assert checkin.listed("Shop A1", db=world.db) is not None
        world.db._conn.execute("UPDATE print_outcomes SET outcome = 'pending' WHERE job_id = 'shift-2'")
        world.db._conn.commit()
        assert checkin.listed("Shop A1", db=world.db) is None
        assert checkin.for_the_ending("Shop A1", db=world.db) is None

    def test_the_watch_carries_it_on_a_finished_print_and_the_wait_tool_reads_it(self, world):
        import inspect

        import kiln.server as srv
        from kiln.plugins.monitoring_tools import _PrintWatcher

        _history(world)
        _print(world, "came-out", days_ago=0.01)
        world.servers.question = _question(about="fixed", ask="Is it fixed?")
        checkin.ask_now("Shop A1", db=world.db)
        watch = SimpleNamespace(_adapter=world.machine, _ENDINGS_THAT_ASK=_PrintWatcher._ENDINGS_THAT_ASK)
        result = _PrintWatcher._with_ending_question(watch, {"outcome": "completed"})
        assert result["ask_the_person"]["question"] == "Is it fixed?"
        # Read once: the stored result is not asked about a second time.
        assert _PrintWatcher._with_ending_question(watch, result) is result
        assert "ask_the_person" not in _PrintWatcher._with_ending_question(watch, {"outcome": "paused"})
        assert "ending_question.attach(result, ending_can_owe=False)" in inspect.getsource(srv.await_print_completion)

    def test_a_cancel_being_sent_never_carries_the_printer_question(self, world):
        _history(world)
        world.servers.question = _question()
        checkin.ask_now("Shop A1", db=world.db)
        result = ending_question.attach({"success": False, "outcome": "failed"}, "Shop A1")
        assert "ask_the_person" not in result

    def test_the_printers_insights_list_it(self, world):
        from kiln.server import mcp

        _history(world)
        world.servers.question = _question()
        checkin.ask_now("Shop A1", db=world.db)
        insights = mcp._tool_manager.get_tool("get_printer_insights").fn(printer_name="Shop A1")
        assert insights["success"] is True and insights["printer_question"]["episode"] == EPISODE
        assert insights["printer_question"]["seen"] == world.servers.question["seen"]
        # Listed there, it is not put to the person again at this ending.
        assert "ask_the_person" not in ending_question.attach({"outcome": "failed"}, "Shop A1")
        other = mcp._tool_manager.get_tool("get_printer_insights").fn(printer_name="Another")
        assert other["printer_question"] is None

    def test_an_endings_own_question_comes_first_and_the_printers_waits(self, world):
        """One question at a time: while an ending owes its question the
        printer's is held back, and it is not spent by being held."""
        from kiln.server import mcp

        _history(world)
        world.servers.question = _question()
        _print(world, "bare", "failed", None, days_ago=5)   # an earlier stop the printer gave no reason for
        checkin.ask_now("Shop A1", db=world.db)
        read = mcp._tool_manager.get_tool("get_printer_insights").fn
        first = read(printer_name="Shop A1")
        assert [r["job_id"] for r in first["endings_to_ask_about"]] == ["bare"]
        assert first["printer_question"] is None
        world.db.mark_ending_asked("bare")
        later = read(printer_name="Shop A1")
        assert later["endings_to_ask_about"] == [] and later["printer_question"]["episode"] == EPISODE


# ---------------------------------------------------------------------------
# Shown once, and again at a new failed ending
# ---------------------------------------------------------------------------


OTHER_EPISODE = "ep:" + "1a2b3c4d5e6f7a8b9c0d1e2f"


class TestShownOnceAndAgainAtANewFailedEnding:
    """Through the tools' own reader (``ending_question.attach``), the
    registered ``get_printer_insights`` and ``set_machine_repair``, the real
    store, and the fake servers."""

    @pytest.fixture
    def asked(self, world):
        """The history, and the question kept and put to the person once."""
        _history(world)
        world.servers.question = _question()
        world.ends = 0
        assert checkin.ask_now("Shop A1", db=world.db)["why"] == "kept"
        assert self._put(world)["episode"] == EPISODE
        assert self._put(world) is None
        return world

    @staticmethod
    def _put(world) -> dict[str, Any] | None:
        """What the tool reporting the printer's newest ending puts to the person."""
        ask = ending_question.attach({"outcome": "completed"}, "Shop A1").get("ask_the_person")
        assert ask is None or ask["about"] == "printer"
        return ask

    @staticmethod
    def _insights(world) -> dict[str, Any] | None:
        from kiln.server import mcp

        return mcp._tool_manager.get_tool("get_printer_insights").fn(printer_name="Shop A1")["printer_question"]

    @staticmethod
    def _ends(world, job: str, outcome: str = "success", mode: str | None = None, *, checks_in: bool = True,
              **more: Any) -> None:
        """One more print ends on the printer, and the check-in for it comes back."""
        world.ends += 1
        _print(world, job, outcome, mode, days_ago=0.5 - 0.01 * world.ends, **more)
        if outcome == "cancelled":
            world.db.mark_ending_asked(job)
        if checks_in:
            assert checkin.ask_now("Shop A1", db=world.db)["why"] == "kept"

    def test_three_prints_that_come_out_do_not_bring_it_back(self, asked):
        for i in range(3):
            self._ends(asked, f"clean-{i}")
            assert asked.db.printer_checkin_waiting(asked.fingerprint)["episode"] == EPISODE
            assert self._put(asked) is None
            assert self._insights(asked)["episode"] == EPISODE
        assert len(asked.servers.calls) == 4

    def test_a_new_failed_print_brings_it_back_once(self, asked):
        self._ends(asked, "clean-0")
        self._ends(asked, "shift-3", "failed", "layer_shift")
        assert self._put(asked)["episode"] == EPISODE
        assert self._put(asked) is None
        self._ends(asked, "clean-1")
        assert self._put(asked) is None

    def test_a_machine_stop_and_a_partial_print_each_bring_it_back(self, asked):
        self._ends(asked, "stall", "failed", "thermal_runaway")
        assert self._put(asked)["episode"] == EPISODE
        self._ends(asked, "half", "partial", "layer_shift")
        assert self._put(asked)["episode"] == EPISODE
        assert self._put(asked) is None

    @pytest.mark.parametrize(
        ("outcome", "mode"),
        [("failed", "power_loss"), ("failed", "filament_runout"), ("cancelled", None), ("unknown", None)],
    )
    def test_an_outside_stop_a_cancel_and_an_unknown_ending_do_not(self, asked, outcome, mode):
        self._ends(asked, "not-the-printer", outcome, mode)
        assert asked.db.printer_checkin_waiting(asked.fingerprint)["episode"] == EPISODE
        assert self._put(asked) is None
        assert self._insights(asked)["episode"] == EPISODE
        assert self._put(asked) is None

    def test_several_failures_bring_it_back_once_each_and_two_unread_bring_it_back_once(self, asked):
        self._ends(asked, "shift-3", "failed", "layer_shift")
        assert self._put(asked)["episode"] == EPISODE
        self._ends(asked, "shift-4", "failed", "layer_shift")
        assert self._put(asked)["episode"] == EPISODE
        assert self._put(asked) is None
        # Two failures with no reader between them are one showing.
        self._ends(asked, "shift-5", "failed", "layer_shift")
        self._ends(asked, "shift-6", "failed", "layer_shift")
        assert self._put(asked)["episode"] == EPISODE
        assert self._put(asked) is None
        self._ends(asked, "clean-0")
        assert self._put(asked) is None

    def test_a_failure_whose_check_in_missed_brings_it_back_at_the_next_ending_that_checks_in(self, asked):
        self._ends(asked, "shift-3", "failed", "layer_shift", checks_in=False)
        assert self._put(asked) is None
        self._ends(asked, "clean-0")
        assert self._put(asked)["episode"] == EPISODE
        assert self._put(asked) is None

    def test_a_failure_the_servers_answer_with_nothing_shows_nothing(self, asked):
        asked.servers.question = None
        self._ends(asked, "shift-3", "failed", "layer_shift")
        assert self._put(asked) is None and self._insights(asked) is None

    def test_a_different_episode_is_a_new_question_shown_once(self, asked):
        asked.servers.question = _question(episode=OTHER_EPISODE, ask="Is it the bed?")
        self._ends(asked, "clean-0")
        assert self._put(asked)["episode"] == OTHER_EPISODE
        assert self._put(asked) is None
        self._ends(asked, "clean-1")
        assert self._put(asked) is None
        # The earlier episode, handed back at a print that came out, stays given.
        asked.servers.question = _question()
        self._ends(asked, "clean-2")
        assert self._insights(asked)["episode"] == EPISODE
        assert self._put(asked) is None

    def test_the_insights_list_it_every_time_and_spend_no_showing(self, asked):
        for _ in range(3):
            assert self._insights(asked)["episode"] == EPISODE
        self._ends(asked, "shift-3", "failed", "layer_shift")
        for _ in range(2):
            assert self._insights(asked)["episode"] == EPISODE
        # Read there after the new failure, it is still put to the person once.
        assert self._put(asked)["episode"] == EPISODE
        assert self._put(asked) is None
        assert self._insights(asked)["episode"] == EPISODE

    def test_a_question_first_met_in_the_insights_is_given_once_there(self, world):
        _history(world)
        world.servers.question = _question()
        world.ends = 0
        checkin.ask_now("Shop A1", db=world.db)
        assert self._insights(world)["episode"] == EPISODE
        assert self._put(world) is None
        self._ends(world, "clean-0")
        assert self._put(world) is None and self._insights(world)["episode"] == EPISODE
        self._ends(world, "shift-3", "failed", "layer_shift")
        assert self._put(world)["episode"] == EPISODE
        assert self._put(world) is None

    def test_an_answer_ends_it_whatever_fails_afterwards(self, asked):
        answer = _tool()("Shop A1", answer_to=EPISODE, answer="neither")
        assert answer["success"] is True and answer["recorded"] is True
        assert self._insights(asked) is None and self._put(asked) is None
        self._ends(asked, "shift-3", "failed", "layer_shift")
        assert self._put(asked) is None and self._insights(asked) is None


# ---------------------------------------------------------------------------
# Answering
# ---------------------------------------------------------------------------


class TestAnswering:
    def _asked(self, world, **changes):
        if world.db.latest_outcome_row("Shop A1") is None:
            _history(world)
        world.servers.question = _question(**changes)
        checkin.ask_now("Shop A1", db=world.db)
        return _tool()

    def test_needs_repair_marks_the_printer_from_the_date_and_part_the_answer_carries(self, world):
        _history(world)
        first_failed = min(
            r["created_at"] for r in world.db.machine_print_rows(world.fingerprint) if r["outcome"] == "failed"
        )
        tool = self._asked(world, answers=[
            {"id": "repair", "say": "It needs repair", "do": "mark_needs_repair", "kind": "belts",
             "since": first_failed},
            {"id": "neither", "say": "Neither", "do": "remember"},
        ])
        since = first_failed
        answer = tool("Shop A1", answer_to=EPISODE, answer="repair")
        assert answer["success"] is True and answer["recorded"] is True and answer["did"] == "mark_needs_repair"
        assert answer["said"] == "It needs repair" and "dated" not in answer
        state = world.db.machine_condition(world.fingerprint)
        assert state["state"] == "needs_repair" and state["kind"] == "belts" and abs(state["since"] - since) < 1
        # The three bad prints of the run are set aside by that mark.
        rows = {r["job_id"]: r for r in world.db.machine_print_rows(world.fingerprint)}
        assert all(world.db.excused_by_repair(rows[f"shift-{i}"]) for i in range(3))
        kept = world.db.get_printer_checkin(EPISODE)
        assert kept["answer"] == "repair" and kept["answered_at"] > time.time() - 5 and kept["about"] == "printer"

    def test_a_date_too_far_back_is_marked_at_the_furthest_a_mark_goes_and_said(self, world):
        too_old = time.time() - 45 * DAY
        tool = self._asked(world, answers=[
            {"id": "repair", "say": "It needs repair", "do": "mark_needs_repair", "kind": "bed", "since": too_old},
            {"id": "neither", "say": "Neither", "do": "remember"},
        ])
        answer = tool("Shop A1", answer_to=EPISODE, answer="repair")
        assert answer["recorded"] is True and "more than 30 days ago" in answer["dated"]
        since = world.db.machine_condition(world.fingerprint)["since"]
        assert abs(since - (time.time() - machine_repairs.BACKDATE_LIMIT_S)) < 120

    def test_a_date_ahead_of_this_clock_is_marked_now_and_said(self, world):
        tool = self._asked(world, answers=[
            {"id": "repair", "say": "It needs repair", "do": "mark_needs_repair", "kind": "bed",
             "since": time.time() + 3600},
            {"id": "neither", "say": "Neither", "do": "remember"},
        ])
        answer = tool("Shop A1", answer_to=EPISODE, answer="repair")
        assert "ahead of this computer's clock" in answer["dated"]
        assert abs(world.db.machine_condition(world.fingerprint)["since"] - time.time()) < 5

    def test_fixed_marks_it_repaired_now(self, world):
        machine_repairs.mark("Shop A1", "needed", kind="bed", db=world.db)
        tool = self._asked(world, about="fixed", ask="Is it fixed?", answers=[
            {"id": "fixed", "say": "Yes, fixed", "do": "mark_repaired", "kind": "bed"},
            {"id": "not_yet", "say": "Not yet", "do": "remember"},
        ])
        answer = tool("Shop A1", answer_to=EPISODE, answer="fixed")
        assert answer["did"] == "mark_repaired" and answer["mark"]["status"] == "done"
        state = world.db.machine_condition(world.fingerprint)
        assert state["state"] == "repaired" and state["kind"] == "bed"
        assert abs(state["repaired_at"] - time.time()) < 5

    @pytest.mark.parametrize("answer_id", ["neither", "filament", "not_yet", "later"])
    def test_remember_writes_no_mark_and_keeps_the_answer(self, world, answer_id):
        tool = self._asked(world, answers=[
            {"id": "repair", "say": "It needs repair", "do": "mark_needs_repair", "kind": "nozzle",
             "since": time.time() - DAY},
            {"id": answer_id, "say": "Something else", "do": "remember"},
        ])
        answer = tool("Shop A1", answer_to=EPISODE, answer=answer_id)
        assert answer["recorded"] is True and answer["did"] == "remember" and "mark" not in answer
        assert world.db.machine_condition(world.fingerprint)["state"] == "ok"
        assert world.db.machine_repair_marks(world.fingerprint) == []
        assert world.db.get_printer_checkin(EPISODE)["answer"] == answer_id

    def test_a_checkup_points_at_this_installs_own_and_marks_nothing(self, world):
        tool = self._asked(world, about="checkup", ask="Want a setup checkup for this printer?", answers=[
            {"id": "checkup", "say": "Yes, show me", "do": "open_checkup"},
            {"id": "later", "say": "Not now", "do": "remember"},
        ])
        answer = tool("Shop A1", answer_to=EPISODE, answer="checkup")
        from kiln.cli_command import kiln_command

        assert answer["did"] == "open_checkup"
        assert answer["checkup"] == {"run": kiln_command("doctor"), "printer": "Shop A1"}
        assert answer["checkup"]["run"].endswith(" doctor") and answer["checkup"]["run"] in answer["next"]
        assert world.db.machine_repair_marks(world.fingerprint) == []
        assert world.db.get_printer_checkin(EPISODE)["answer"] == "checkup"

    def test_an_answered_episode_goes_up_and_is_never_shown_again(self, world):
        tool = self._asked(world)
        tool("Shop A1", answer_to=EPISODE, answer="neither")
        assert checkin.listed("Shop A1", db=world.db) is None
        _print(world, "shift-3", "failed", "layer_shift", days_ago=0.1)
        checkin.ask_now("Shop A1", db=world.db)
        answered = world.servers.calls[-1]["body"]["answered"]
        assert [a["key"] for a in answered] == [EPISODE] and abs(answered[0]["at"] - time.time()) < 10
        # Servers that hand the same episode back are not obeyed.
        assert checkin.listed("Shop A1", db=world.db) is None
        assert "ask_the_person" not in ending_question.attach({"outcome": "failed"}, "Shop A1")
        # Nor is an answered row that is made to wait by hand.
        world.db._conn.execute("UPDATE printer_checkins SET asked_job_id = 'shift-3'")
        world.db._conn.commit()
        assert world.db.printer_checkin_waiting(world.fingerprint) is None
        assert checkin.listed("Shop A1", db=world.db) is None
        again = tool("Shop A1", answer_to=EPISODE, answer="repair")
        assert again["recorded"] is False and again["already_answered"] is True and again["answer"] == "neither"
        assert world.db.machine_condition(world.fingerprint)["state"] == "ok"

    def test_what_the_door_refuses_and_keeps_nothing_for(self, world):
        tool = self._asked(world)
        unknown = tool("Shop A1", answer_to="ep:" + "0" * 24, answer="repair")
        assert unknown["success"] is False and unknown["error"]["code"] == checkin.CODE_UNKNOWN
        bad = tool("Shop A1", answer_to=EPISODE, answer="filament")
        assert bad["success"] is False and bad["error"]["code"] == checkin.CODE_BAD_ANSWER
        both = tool("Shop A1", "needed", answer_to=EPISODE, answer="repair")
        assert both["success"] is False and both["error"]["code"] == "REPAIR_ANSWER_AMBIGUOUS"
        half = tool("Shop A1", answer_to=EPISODE)
        assert half["success"] is False and half["error"]["code"] == "REPAIR_ANSWER_AMBIGUOUS"
        assert world.db.get_printer_checkin(EPISODE)["answer"] is None
        assert world.db.machine_repair_marks(world.fingerprint) == []

    def test_kilns_own_servers_keep_no_answer(self, world, monkeypatch):
        import kiln.runtime_env as runtime_env

        tool = self._asked(world)
        monkeypatch.setattr(runtime_env, "is_hosted_multitenant", lambda: True)
        refused = tool("Shop A1", answer_to=EPISODE, answer="neither")
        assert refused["success"] is False and refused["error"]["code"] == "LOCAL_ONLY"
        assert world.db.get_printer_checkin(EPISODE)["answer"] is None

    def test_a_question_about_another_printer_is_refused(self, world):
        self._asked(world)
        other = _machine("Shop A1")
        other.serial = "01P00A000000099"
        world.machine.serial = other.serial  # the name now answers to a different machine
        with pytest.raises(machine_repairs.RepairRefused) as refused:
            checkin.answer("Shop A1", EPISODE, "repair", db=world.db)
        assert refused.value.code == checkin.CODE_OTHER_PRINTER
        assert world.db.get_printer_checkin(EPISODE)["answer"] is None


    def test_the_tool_reporting_an_ending_never_opens_the_record(self, world, monkeypatch):
        _history(world)
        world.servers.question = _question()
        checkin.ask_now("Shop A1", db=world.db)
        opened: list[int] = []
        monkeypatch.setattr(persistence_mod, "_db", None, raising=False)
        monkeypatch.setattr(persistence_mod, "KilnDB", lambda *a, **k: opened.append(1) or world.db)
        assert persistence_mod.db_if_open() is None
        assert ending_question.attach({"outcome": "completed"}, "Shop A1", ending_can_owe=False) == {"outcome": "completed"}
        assert opened == []
        monkeypatch.setattr(persistence_mod, "_db", world.db, raising=False)
        shown = ending_question.attach({"outcome": "completed"}, "Shop A1", ending_can_owe=False)
        assert shown["ask_the_person"]["episode"] == EPISODE


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


def test_a_database_made_before_the_table_gains_it(tmp_path):
    path = str(tmp_path / "old.db")
    db = KilnDB(db_path=path)
    db._conn.execute("DROP TABLE printer_checkins")
    db._conn.commit()
    db.close()
    db = KilnDB(db_path=path)
    db.set_printer_checkin("fp", "p", _question(), asked_job_id="j")
    assert db.printer_checkin_waiting("fp")["episode"] == EPISODE
    assert db.printer_checkins_answered("fp") == []
    db.close()


def test_signing_in_forgets_what_was_asked_while_signed_out():
    """A question answered "signed out" is not remembered past the sign-in,
    so the next ending asks Kiln's servers for the account now signed in."""
    from kiln import _pro_printer_checkin_bridge as checkin
    from kiln.cli.auth_commands import _complete_signin

    checkin._asked["bambu"] = "signed-out-state"
    try:
        _complete_signin({"access_token": "a", "refresh_token": "r", "email": "x@y.z"}, door="chat")
        assert checkin._asked == {}
    finally:
        checkin.forget()
