"""Renaming a saved printer carries everything filed under its name.

A printer's name keys its history, what is loaded in it, its queued jobs,
its stop latch, the retained print, and the standing permissions that
name it.  These tests seed each of those under one name, rename the
printer through the shared door and through both doors a person meets
(the tool and the terminal command), and read every store back by the new
name -- behaviour, never structure.  The atomic case breaks one store in
the middle and reads the old name back whole.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import kiln.server as srv
from kiln import consent_windows, monitor_twin, persistence
from kiln.cli import config as cli_config
from kiln.cli.main import cli
from kiln.consent_windows import SOURCE_TERMINAL, Window
from kiln.emergency import EmergencyCoordinator
from kiln.plugins.printer_management_tools import _PrinterManagementToolsPlugin
from kiln.printer_rename import RenameRefused, rename_printer
from kiln.printers import engagement
from kiln.printers.base import PrinterStatus
from kiln.queue import PrintQueue
from kiln.registry import PrinterRegistry
from tests.test_refusals_speak_for_themselves import _Printer

OLD, NEW = "shop-a1", "kitchen-a1"


@pytest.fixture
def bench(monkeypatch, tmp_path):
    """One saved, registered, idle printer with records under its name."""
    home = tmp_path / "kiln-home"
    home.mkdir()
    monkeypatch.setenv("KILN_HOME", str(home))

    config_path = home / "config.yaml"
    cli_config._write_config_file(
        config_path,
        {
            "printers": {
                OLD: {"type": "bambu", "host": "10.0.0.5", "serial": "AAA111", "access_code": "12345678",
                      "printer_model": "A1"},
                "ender": {"type": "octoprint", "host": "http://10.0.0.9", "api_key": "k"},
            },
            "active_printer": OLD,
        },
    )
    monkeypatch.setattr(cli_config, "get_config_path", lambda: config_path)

    monkeypatch.setenv("KILN_DB_PATH", str(home / "kiln.db"))
    db = persistence.get_db()
    assert db._db_path == str(home / "kiln.db")
    db._conn.execute(
        "INSERT INTO print_outcomes (job_id, printer_name, file_name, outcome, created_at) VALUES (?, ?, ?, ?, ?)",
        ("j1", OLD, "bracket.gcode", "success", time.time()),
    )
    db._conn.execute(
        "INSERT INTO print_history (job_id, printer_name, file_name, status, created_at) VALUES (?, ?, ?, ?, ?)",
        ("j1", OLD, "bracket.gcode", "completed", time.time()),
    )
    db._conn.execute(
        "INSERT INTO printer_materials (printer_name, tool_index, material_type, loaded_at) VALUES (?, 0, 'PLA', ?)",
        (OLD, time.time()),
    )
    db._conn.commit()

    queue = PrintQueue(db_path=str(home / "queue.db"))
    queue.submit("next.gcode", printer_name=OLD)
    monkeypatch.setattr(srv, "_get_queue", lambda: queue)

    printer = _Printer("AAA111", PrinterStatus.IDLE, file=None)
    registry = PrinterRegistry()
    registry.register(OLD, printer)
    registry.register("ender", _Printer("BBB222", PrinterStatus.IDLE, file=None))
    monkeypatch.setattr(srv, "_get_registry", lambda: registry)
    monkeypatch.setattr(srv, "_adapter", printer)
    monkeypatch.setattr(srv, "_check_auth", lambda *_a, **_k: None)

    coordinator = EmergencyCoordinator()
    monkeypatch.setattr("kiln.emergency._coordinator", coordinator)
    with coordinator._lock:
        # A cleared stop: the record of one, no longer latched.
        latch = coordinator._ensure_latch_locked(OLD)
        latch.latched, latch.reason = False, "cleared earlier"
        coordinator._persist_state_locked()
    assert not coordinator.is_stopped(OLD) and OLD in coordinator._latches

    twin_dir = home / "monitor_twin"
    twin_dir.mkdir()
    monkeypatch.setattr(monitor_twin, "_TWIN_DIR", twin_dir)
    monkeypatch.setattr(monitor_twin, "_ACTIVE_FILE", twin_dir / "active.json")
    gcode = twin_dir / f"{monitor_twin._slug(OLD)}-current.gcode"
    gcode.write_text("G28\n")
    (twin_dir / "active.json").write_text(json.dumps({
        monitor_twin._slug(OLD): {"file_name": "bracket.gcode", "printer_name": OLD, "started_at": "2026-11-01T00:00:00Z",
                                  "gcode": str(gcode), "mesh": None},
    }))

    machine = consent_windows.machine_under(OLD)
    assert machine, "the stand-in printer reports a serial, so it is identifiable"
    consent_windows._reset_for_tests()
    consent_windows._write([
        Window(id="w_timed", set_by="os_user:adam", set_at=time.time(), until=time.time() + 3600, scope=(OLD,),
               source=SOURCE_TERMINAL),
        Window(id="w_always", set_by="os_user:adam", set_at=time.time(), until=None, scope=(OLD,),
               source=SOURCE_TERMINAL, always=True, machine=machine),
    ])
    engagement.engage(printer, reason="started", label=OLD)
    assert engagement.current() is not None and engagement.current().label == OLD

    db._conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (f"leveling_policy:{OLD}", '{"mode": "always"}'))
    db._conn.commit()
    from kiln import nozzle_milestones

    nozzle_milestones._write_all({OLD: {"rung": "exceeded_p50", "grams": 1200.0, "material": "brass", "at": "2026-11-01T00:00:00Z"}})
    monkeypatch.setattr("kiln.bridge_client.account_signed_out", lambda: True)
    monkeypatch.setattr("kiln.bridge_client.delegations_naming", lambda name: None)
    monkeypatch.setattr("kiln.bridge_client.always_allow_grants", lambda: None)
    monkeypatch.setattr("kiln.bridge_client.rename_on_account", lambda old, new: None)

    return {"home": home, "config": config_path, "db": db, "queue": queue, "registry": registry,
            "printer": printer, "coordinator": coordinator, "machine": machine, "twin_dir": twin_dir}


def _saved(config_path: Path) -> dict[str, Any]:
    return cli_config._read_config_file(config_path)


class TestEverythingFollowsTheName:
    def test_history_settings_and_permissions_follow(self, bench):
        result = rename_printer(OLD, NEW, source="person")

        assert result["success"] is True
        raw = _saved(bench["config"])
        assert list(raw["printers"]) == [NEW, "ender"], "the entry keeps its place"
        assert raw["printers"][NEW]["serial"] == "AAA111" and raw["active_printer"] == NEW

        db = bench["db"]
        assert not any(db.rows_under_printer_name(OLD).values())
        under_new = db.rows_under_printer_name(NEW)
        assert under_new["print_outcomes"] == 1 and under_new["print_history"] == 1
        assert under_new["printer_materials"] == 1
        assert db.get_material(NEW, 0)["material_type"] == "PLA"
        formerly = db.printer_renames(NEW)
        assert [(r["old_name"], r["new_name"], r["source"]) for r in formerly] == [(OLD, NEW, "person")]
        assert formerly[0]["machine_fingerprint"] == bench["machine"]
        assert result["formerly"] == formerly

        [job] = bench["queue"].list_jobs()
        assert job.printer_name == NEW
        assert bench["queue"]._db.execute("SELECT printer_name FROM jobs").fetchone()[0] == NEW

        coordinator = bench["coordinator"]
        assert NEW in coordinator._latches and OLD not in coordinator._latches
        assert coordinator._latches[NEW].reason == "cleared earlier"
        reloaded = EmergencyCoordinator()
        assert NEW in reloaded._latches and OLD not in reloaded._latches, "the latch record is persisted under the new name"

        twin = monitor_twin.active_twin(NEW)
        assert twin and twin["printer_name"] == NEW and os.path.isfile(twin["gcode"])
        assert monitor_twin.active_twin(OLD) is None

        assert engagement.current().label == NEW

        registry = bench["registry"]
        assert NEW in registry and OLD not in registry
        assert registry.get(NEW) is bench["printer"]
        assert bench["printer"]._kiln_registered_name == NEW

        scopes = {w.id: w.scope for w in consent_windows.live_windows()}
        assert scopes == {"w_timed": (NEW,), "w_always": (NEW,)}
        assert result["permissions"]["followed"] == ["w_timed", "w_always"] and result["permissions"]["closed"] == []
        [always] = [w for w in consent_windows.live_windows() if w.always]
        assert always.scope == (NEW,) and always.machine == bench["machine"], "the same machine, the new name"

        assert {c["store"] for c in result["carried"]} >= {"config", "records", "queue", "emergency_stop",
                                                           "retained_print", "engagement", "registry",
                                                           "nozzle_milestone"}
        assert "came with it" in result["message"]
        assert db.get_setting(f"leveling_policy:{NEW}") == '{"mode": "always"}' and db.get_setting(f"leveling_policy:{OLD}") is None
        from kiln import nozzle_milestones

        assert nozzle_milestones.is_flagged(NEW) and not nozzle_milestones.is_flagged(OLD)
        assert any("Not signed in" in w for w in result["warnings"]), "signed out: the account step says so"

    def test_the_other_printer_is_untouched(self, bench):
        rename_printer(OLD, NEW)
        raw = _saved(bench["config"])
        assert raw["printers"]["ender"] == {"type": "octoprint", "host": "http://10.0.0.9", "api_key": "k"}
        assert "ender" in bench["registry"]

    def test_a_second_rename_keeps_the_whole_chain(self, bench):
        rename_printer(OLD, NEW)
        rename_printer(NEW, "garage-a1")
        chain = bench["db"].printer_renames("garage-a1")
        assert [(r["old_name"], r["new_name"]) for r in chain] == [(OLD, NEW), (NEW, "garage-a1")]
        assert bench["db"].rows_under_printer_name("garage-a1")["print_outcomes"] == 1


class TestWhatIsRefused:
    def test_a_printing_printer_is_refused_and_nothing_moves(self, bench):
        bench["printer"].status = PrinterStatus.PRINTING
        with pytest.raises(RenameRefused) as caught:
            rename_printer(OLD, NEW)
        assert caught.value.code == "PRINTER_BUSY"
        assert OLD in _saved(bench["config"])["printers"]
        assert bench["db"].rows_under_printer_name(OLD)["print_outcomes"] == 1
        assert OLD in bench["registry"]

    def test_a_taken_name_is_refused(self, bench):
        with pytest.raises(RenameRefused) as caught:
            rename_printer(OLD, "Ender")
        assert caught.value.code == "NAME_TAKEN"

    def test_a_name_with_records_of_its_own_is_refused(self, bench):
        bench["db"]._conn.execute(
            "INSERT INTO print_outcomes (job_id, printer_name, outcome, created_at) VALUES ('j9', ?, 'failed', ?)",
            ("retired-a1", time.time()),
        )
        bench["db"]._conn.commit()
        with pytest.raises(RenameRefused) as caught:
            rename_printer(OLD, "retired-a1")
        assert caught.value.code == "NAME_TAKEN" and "merge" in caught.value.message

    def test_a_name_with_queued_jobs_of_its_own_is_refused(self, bench):
        bench["queue"].submit("theirs.gcode", printer_name="retired-a1")
        with pytest.raises(RenameRefused) as caught:
            rename_printer(OLD, "retired-a1")
        assert caught.value.code == "NAME_TAKEN" and "queue" in caught.value.message
        assert OLD in _saved(bench["config"])["printers"]

    def test_a_printer_under_an_emergency_stop_is_refused(self, bench):
        coordinator = bench["coordinator"]
        with coordinator._lock:
            coordinator._ensure_latch_locked(OLD).latched = True
            coordinator._stopped_printers.add(OLD)
        with pytest.raises(RenameRefused) as caught:
            rename_printer(OLD, NEW)
        assert caught.value.code == "EMERGENCY_STOPPED" and "emergency-clear" in caught.value.message
        assert coordinator.is_stopped(OLD) and OLD in _saved(bench["config"])["printers"]

    def test_a_name_a_standing_permission_still_covers_is_refused(self, bench):
        consent_windows._write([
            *consent_windows.all_windows(),
            Window(id="w_stale", set_by="os_user:adam", set_at=time.time(), until=time.time() + 3600,
                   scope=("Kitchen-A1",), source=SOURCE_TERMINAL),
        ])
        with pytest.raises(RenameRefused) as caught:
            rename_printer(OLD, NEW)
        assert caught.value.code == "NAME_TAKEN" and caught.value.extra["windows"] == ["w_stale"]
        assert "different machine" in caught.value.message
        assert OLD in _saved(bench["config"])["printers"]

    def test_a_name_differing_only_by_case_from_another_printer_is_refused(self, bench):
        with pytest.raises(RenameRefused) as caught:
            rename_printer(OLD, "ENDER")
        assert caught.value.code == "NAME_TAKEN"

    def test_a_reservation_held_by_another_session_refuses(self, bench):
        from kiln import printer_lock

        key = printer_lock.machine_key(bench["printer"], OLD)
        with printer_lock._exclusive():
            printer_lock._write({key: {"label": OLD, "holder": "codex", "session": "other-session", "pid": 1,
                                       "acquired_at": time.time(), "expires_at": time.time() + 600}})
        with pytest.raises(RenameRefused) as caught:
            rename_printer(OLD, NEW)
        assert caught.value.code == "PRINTER_BUSY" and "codex" in caught.value.message
        assert OLD in _saved(bench["config"])["printers"]

    def test_a_former_name_is_refused_whatever_its_case(self, bench):
        rename_printer(OLD, NEW)
        with pytest.raises(RenameRefused) as caught:
            rename_printer(NEW, OLD.upper(), source="agent")
        assert caught.value.code == "NAME_FORMER"

    def test_a_former_name_is_refused_to_an_agent_and_said_to_a_person(self, bench):
        rename_printer(OLD, NEW)
        bench["printer"].status = PrinterStatus.IDLE
        with pytest.raises(RenameRefused) as caught:
            rename_printer(NEW, OLD, source="agent")
        assert caught.value.code == "NAME_FORMER"
        result = rename_printer(NEW, OLD, source="person")
        assert result["success"] and any("account" in w for w in result["warnings"])
        assert "account page" in result["message"]

    def test_kilns_own_alias_is_refused(self, bench):
        with pytest.raises(RenameRefused) as caught:
            rename_printer(OLD, "default")
        assert caught.value.code == "NAME_RESERVED"

    def test_kilns_own_alias_is_not_renamed_from_either(self, bench):
        bench["registry"].register("default", bench["printer"])
        with pytest.raises(RenameRefused) as caught:
            rename_printer("default", "workshop")
        assert caught.value.code == "NAME_RESERVED" and "default" in bench["registry"]

    def test_a_terminal_with_an_empty_registry_still_asks_the_printer(self, bench, monkeypatch):
        """A fresh `kiln rename-printer` process holds no registered printers;
        the door builds the adapter from config and asks it anyway."""
        bench["printer"].status = PrinterStatus.PRINTING
        empty = PrinterRegistry()
        monkeypatch.setattr(srv, "_get_registry", lambda: empty)
        monkeypatch.setattr(srv, "_resolve_adapter", lambda name=None: bench["printer"] if name in (OLD, None) else (_ for _ in ()).throw(KeyError(name)))
        with pytest.raises(RenameRefused) as caught:
            rename_printer(OLD, NEW)
        assert caught.value.code == "PRINTER_BUSY"
        assert OLD in _saved(bench["config"])["printers"]

    def test_a_case_only_rename_is_a_rename(self, bench):
        result = rename_printer(OLD, OLD.upper())
        assert result["success"] and OLD.upper() in _saved(bench["config"])["printers"]
        assert bench["db"].rows_under_printer_name(OLD.upper())["print_outcomes"] == 1
        assert {w.scope for w in consent_windows.live_windows()} == {(OLD.upper(),)}
        assert monitor_twin.active_twin(OLD.upper())["printer_name"] == OLD.upper()

    def test_an_unknown_printer_is_refused_by_name(self, bench):
        with pytest.raises(RenameRefused) as caught:
            rename_printer("nobody", NEW)
        assert caught.value.code == "PRINTER_NOT_FOUND" and "ender" in caught.value.message

    def test_a_name_carrying_the_address_is_refused_with_a_clean_one(self, bench):
        with pytest.raises(RenameRefused) as caught:
            rename_printer(OLD, "kitchen 10.0.0.5")
        assert caught.value.code == "NAME_HAS_IDENTIFIER"
        assert caught.value.extra["suggested_name"] and "10.0.0.5" not in caught.value.extra["suggested_name"]
        assert OLD in _saved(bench["config"])["printers"]


class TestAllOrNothing:
    def test_a_failure_inside_the_records_transaction_leaves_every_row(self, bench):
        db = bench["db"]
        db._conn.execute("DROP TABLE printer_renames")
        db._conn.commit()
        with pytest.raises(RenameRefused) as caught:
            rename_printer(OLD, NEW)
        assert caught.value.code == "RENAME_FAILED" and "Everything was put back" in caught.value.message
        assert db.rows_under_printer_name(OLD)["print_outcomes"] == 1 and not any(db.rows_under_printer_name(NEW).values())
        # The connection is usable and holds no half-applied transaction.
        db.set_setting("probe", "1")
        assert db.rows_under_printer_name(OLD)["print_outcomes"] == 1
        assert list(_saved(bench["config"])["printers"]) == [OLD, "ender"]

    def test_a_store_that_fails_puts_every_earlier_one_back(self, bench, monkeypatch):
        def _broken(old, new):
            raise OSError("disk full")

        monkeypatch.setattr(monitor_twin, "rename_printer", _broken)
        with pytest.raises(RenameRefused) as caught:
            rename_printer(OLD, NEW)
        assert caught.value.code == "RENAME_FAILED" and "put back" in caught.value.message

        raw = _saved(bench["config"])
        assert list(raw["printers"]) == [OLD, "ender"] and raw["active_printer"] == OLD
        db = bench["db"]
        assert db.rows_under_printer_name(OLD)["print_outcomes"] == 1
        assert not any(db.rows_under_printer_name(NEW).values())
        assert db.printer_renames(NEW) == [], "no ledger row for a rename that did not happen"
        [job] = bench["queue"].list_jobs()
        assert job.printer_name == OLD
        assert OLD in bench["coordinator"]._latches and NEW not in bench["coordinator"]._latches
        assert OLD in bench["registry"] and NEW not in bench["registry"]
        assert {w.scope for w in consent_windows.live_windows()} == {(OLD,)}


class TestStandingPermissions:
    def test_always_allow_for_another_machine_is_closed_not_moved(self, bench):
        consent_windows._write([
            Window(id="w_other", set_by="os_user:adam", set_at=time.time(), until=None, scope=(OLD,),
                   source=SOURCE_TERMINAL, always=True, machine="_printer:serial:zzz999"),
        ])
        result = rename_printer(OLD, NEW)
        assert result["permissions"] == {"followed": [], "closed": ["w_other"]}
        [w] = consent_windows.all_windows()
        assert w.revoked_at is not None and w.revoked_reason == consent_windows.REASON_PRINTER_RENAMED
        assert "turned off" in result["message"]

    def test_the_accounts_copy_is_closed_and_the_account_told_why(self, bench, monkeypatch):
        told: list[tuple[str, str]] = []
        monkeypatch.setattr(
            "kiln.bridge_client.revoke_on_account", lambda grant_id, *, reason="": told.append((grant_id, reason)) or True
        )
        consent_windows._write([
            Window(id="w_copy", set_by="account:adam", set_at=time.time(), until=None, scope=(OLD,),
                   source=SOURCE_TERMINAL, always=True, machine=bench["machine"], account_grant="g_1"),
        ])
        result = rename_printer(OLD, NEW)
        assert result["permissions"]["closed"] == ["w_copy"]
        assert told == [("g_1", "printer_renamed")]


class TestTheAccount:
    def test_signed_in_the_accounts_records_follow_and_undo_tells_it_back(self, bench, monkeypatch):
        calls: list[tuple[str, str]] = []
        monkeypatch.setattr("kiln.bridge_client.account_signed_out", lambda: False)
        monkeypatch.setattr("kiln.bridge_client.delegations_naming", lambda name: [])
        monkeypatch.setattr("kiln.bridge_client.always_allow_grants", lambda: [])
        monkeypatch.setattr("kiln.bridge_client.rename_on_account", lambda old, new: calls.append((old, new)) or (True, ""))
        result = rename_printer(OLD, NEW)
        assert calls == [(OLD, NEW)] and {"store": "account", "moved": 1} in result["carried"]

        def _broken(old, new, *, machine):
            raise OSError("disk full")

        monkeypatch.setattr(consent_windows, "rename_printer", _broken)  # the step after the account's
        with pytest.raises(RenameRefused):
            rename_printer(NEW, "garage-a1")
        assert calls[-1] == ("garage-a1", NEW), "the account is told to rename back"

    def test_signed_in_but_unreachable_is_refused_before_anything_moves(self, bench, monkeypatch):
        monkeypatch.setattr("kiln.bridge_client.account_signed_out", lambda: False)
        with pytest.raises(RenameRefused) as caught:
            rename_printer(OLD, NEW, source="agent")
        assert caught.value.code == "ACCOUNT_UNREACHABLE"
        assert OLD in _saved(bench["config"])["printers"]
        assert bench["db"].rows_under_printer_name(OLD)["print_outcomes"] == 1

    def test_signed_in_and_the_records_step_fails_is_refused_whole(self, bench, monkeypatch):
        monkeypatch.setattr("kiln.bridge_client.account_signed_out", lambda: False)
        monkeypatch.setattr("kiln.bridge_client.delegations_naming", lambda name: [])
        monkeypatch.setattr("kiln.bridge_client.always_allow_grants", lambda: [])
        monkeypatch.setattr("kiln.bridge_client.rename_on_account", lambda old, new: (False, "'kitchen-a1' already has a nozzle record on this account"))
        with pytest.raises(RenameRefused) as caught:
            rename_printer(OLD, NEW)
        assert caught.value.code == "RENAME_FAILED" and "nozzle record" in caught.value.message
        assert OLD in _saved(bench["config"])["printers"]
        assert bench["db"].rows_under_printer_name(OLD)["print_outcomes"] == 1

    def test_a_delegation_naming_the_new_name_refuses_and_one_naming_the_old_is_reported(self, bench, monkeypatch):
        rows = {NEW: [{"id": "d1", "agent": "Claude on the laptop", "until": None}], OLD: []}
        monkeypatch.setattr("kiln.bridge_client.delegations_naming", lambda name: rows.get(name, []))
        with pytest.raises(RenameRefused) as caught:
            rename_printer(OLD, NEW)
        assert caught.value.code == "NAME_TAKEN" and "Claude on the laptop" in caught.value.message
        rows[NEW], rows[OLD] = [], [{"id": "d2", "agent": "the night agent", "until": None}]
        result = rename_printer(OLD, NEW)
        assert "the night agent" in result["permissions"]["account"] and result["permissions"]["account_delegations"][0]["id"] == "d2"


    def test_always_allow_on_the_account_for_the_new_name_refuses(self, bench, monkeypatch):
        monkeypatch.setattr("kiln.bridge_client.delegations_naming", lambda name: [])
        monkeypatch.setattr(
            "kiln.bridge_client.always_allow_grants",
            lambda: [{"id": "g9", "printer": "Kitchen-A1", "grantor": "account:adam", "issued_at": 1.0}],
        )
        with pytest.raises(RenameRefused) as caught:
            rename_printer(OLD, NEW)
        assert caught.value.code == "NAME_TAKEN" and caught.value.extra["always_allow"] == ["g9"]
        assert OLD in _saved(bench["config"])["printers"]

    def test_the_shared_server_refuses_at_the_door_itself(self, bench, monkeypatch):
        monkeypatch.setattr("kiln.runtime_env.is_hosted_multitenant", lambda: True)
        with pytest.raises(RenameRefused) as caught:
            rename_printer(OLD, NEW)
        assert caught.value.code == "NOT_HERE"
        assert OLD in _saved(bench["config"])["printers"]


class TestARunningProcessFollows:
    def test_the_registry_and_queue_of_another_process_follow_the_ledger(self, bench, monkeypatch):
        other = PrinterRegistry()
        other.register(OLD, bench["printer"])
        other_queue = PrintQueue()
        other_queue.submit("x.gcode", printer_name=OLD)
        monkeypatch.setattr(srv, "_queue", other_queue)
        srv._renames_seen.update({"mtime": None, "id": None})
        srv._sync_printer_renames(other)  # first look: nothing to apply
        from kiln.emergency import EmergencyCoordinator

        theirs = EmergencyCoordinator()  # the other process's, loaded from disk: a cleared stop under OLD
        assert OLD in theirs._latches
        rename_printer(OLD, NEW)  # this process's coordinator (the bench's) moved its own record
        monkeypatch.setattr("kiln.emergency._coordinator", theirs)
        # Between the rename and this process's next look, it latched a stop
        # under the old name: the stop follows the name, latched.
        with theirs._lock:
            theirs._ensure_latch_locked(OLD).latched = True
            theirs._stopped_printers.add(OLD)
        srv._sync_printer_renames(other)
        assert NEW in other and OLD not in other
        assert [j.printer_name for j in other_queue.list_jobs()] == [NEW]
        assert theirs.is_stopped(NEW) and not theirs.is_stopped(OLD), "a stop stays a stop, under the new name"


class TestTheDoors:
    def _tool(self):
        captured: dict[str, Any] = {}

        class _Mcp:
            def tool(self, **_kwargs):
                return lambda fn: captured.setdefault(fn.__name__, fn)

        _PrinterManagementToolsPlugin().register(_Mcp())
        return captured["rename_printer"]

    def test_the_tool_renames_and_says_so(self, bench):
        answer = self._tool()(printer_name=OLD, new_name=NEW)
        assert answer["success"] is True and answer["new_name"] == NEW
        assert bench["db"].rows_under_printer_name(NEW)["print_outcomes"] == 1
        assert bench["db"].printer_renames(NEW)[0]["source"] == "agent"

    def test_the_tool_refuses_in_the_envelope(self, bench):
        bench["printer"].status = PrinterStatus.PRINTING
        answer = self._tool()(printer_name=OLD, new_name=NEW)
        assert answer["success"] is False and answer["error"]["code"] == "PRINTER_BUSY"

    def test_the_tool_moves_a_session_only_registration(self, bench):
        srv._SESSION_PRINTERS[OLD] = {"type": "bambu", "host": "10.0.0.5"}
        try:
            self._tool()(printer_name=OLD, new_name=NEW)
            assert NEW in srv._SESSION_PRINTERS and OLD not in srv._SESSION_PRINTERS
        finally:
            srv._SESSION_PRINTERS.pop(NEW, None)
            srv._SESSION_PRINTERS.pop(OLD, None)

    def test_the_command_renames_and_prints_json(self, bench):
        out = CliRunner().invoke(cli, ["rename-printer", OLD, NEW, "--json"])
        assert out.exit_code == 0, out.output
        body = json.loads(out.output)
        assert body["status"] == "success" and body["data"]["new_name"] == NEW
        assert _saved(bench["config"])["active_printer"] == NEW
        assert bench["db"].printer_renames(NEW)[0]["source"] == "person"

    def test_account_only_moves_the_accounts_records_and_nothing_here(self, bench, monkeypatch):
        calls = []
        monkeypatch.setattr("kiln.bridge_client.account_signed_out", lambda: False)
        monkeypatch.setattr("kiln.bridge_client.rename_on_account", lambda old, new: calls.append((old, new)) or (True, ""))
        out = CliRunner().invoke(cli, ["rename-printer", OLD, NEW, "--account-only", "--json"])
        assert out.exit_code == 0, out.output
        assert calls == [(OLD, NEW)] and OLD in _saved(bench["config"])["printers"]

    def test_account_only_signed_out_is_refused(self, bench):
        out = CliRunner().invoke(cli, ["rename-printer", OLD, NEW, "--account-only"])
        assert out.exit_code != 0 and "Sign in" in out.output

    def test_the_command_refuses_in_words(self, bench):
        out = CliRunner().invoke(cli, ["rename-printer", OLD, "Ender"])
        assert out.exit_code != 0 and "already set up" in out.output
        assert OLD in _saved(bench["config"])["printers"]
