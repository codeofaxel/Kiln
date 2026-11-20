"""A print the person asks not to keep leaves nothing behind once it ends.

The person can ask, for one print, that Kiln keep no record of it.  While
it prints, only the running print's start record lives on this computer
(what the mid-print checks and a power-loss resume read); when it ends that
record goes too, and no row, token, pooled contribution or account copy of
the print is ever written.  The account's own choice keeps history on this
computer only: rows stay here and nothing about them leaves.  A team's rule
keeps the team's designs on the team's printers, and says so before
anything reaches the printer.

Every test drives the registered ``start_print`` tool and the adapter's
own status read, the doors a print really passes, with a stand-in printer.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiln import monitor_twin, print_keeping


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KILN_HOME", str(tmp_path / ".kiln"))
    monkeypatch.setenv("KILN_DB_PATH", str(tmp_path / "kiln.db"))
    monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")
    monkeypatch.setenv("KILN_SKIP_PREFLIGHT", "1")
    twin = tmp_path / ".kiln" / "monitor_twin"
    monkeypatch.setattr(monitor_twin, "_TWIN_DIR", twin)
    monkeypatch.setattr(monitor_twin, "_SLICES_FILE", twin / "slices.json")
    monkeypatch.setattr(monitor_twin, "_ACTIVE_FILE", twin / "active.json")
    import kiln.persistence as _p

    monkeypatch.setattr(_p, "_db", None, raising=False)
    print_keeping._ended_not_kept.clear()
    pro = False
    try:
        from kiln_pro.print_learning import auto_record as _pro_auto_record

        pro = _pro_auto_record.uninstall_auto_record_hook()
    except ImportError:
        pass
    with patch("kiln.server._check_rate_limit", return_value=None):
        yield tmp_path
    if pro:
        _pro_auto_record.install_auto_record_hook()
    print_keeping._ended_not_kept.clear()
    monkeypatch.setattr(_p, "_db", None, raising=False)


def _printer(name: str = "shop-x1", job_file: str = "secret_part.gcode"):
    from kiln.printers.base import JobProgress, PrinterAdapter, PrinterState, PrinterStatus, PrintResult

    class _Fake(PrinterAdapter):
        _DURATION_SEMANTICS = "frozen"

        def __init__(self):
            self.state_word = "idle"
            self.sent: list[str] = []
            self._kiln_registered_name = name

        def _start_print_impl(self, file_name: str, **kwargs: Any) -> PrintResult:
            self.sent.append(file_name)
            self.state_word = "printing"
            return PrintResult(success=True, message=f"Started {file_name}.")

        def get_state(self):
            return PrinterState(connected=True, state=PrinterStatus(self.state_word))

        def get_job(self):
            return JobProgress(file_name=job_file, print_time_seconds=1800)

        def connect(self): ...
        def disconnect(self): ...
        def get_printer_info(self): ...
        def list_files(self):
            return []
        def upload_file(self, file_path): ...
        def cancel_print(self): ...
        def pause_print(self): ...
        def resume_print(self): ...

    _Fake.name = name
    _Fake.__abstractmethods__ = frozenset()
    return _Fake()


def _start(adapter, file_name: str = "secret_part.gcode", **kw):
    from kiln.server import start_print

    registry = MagicMock()
    registry.count = 1
    registry.list_names.return_value = [adapter._kiln_registered_name]
    with patch("kiln.server._get_registry", return_value=registry), \
         patch("kiln.server._get_adapter", return_value=adapter), \
         patch("kiln.server._resolve_adapter", return_value=adapter, create=True), \
         patch("kiln.server._resolve_effective_printer_name", return_value=adapter._kiln_registered_name):
        return start_print(file_name, **kw)


def _end(adapter, word: str = "idle") -> None:
    """The printer reports it is printing, then the ending: the edge every
    door's status read feeds into the print's record."""
    adapter.state_word = "printing"
    adapter.get_state()
    adapter.state_word = word
    adapter.get_state()


def _rows() -> list[dict[str, Any]]:
    from kiln.persistence import get_db

    return get_db().list_print_outcomes(limit=50, include_all=True)


def _everything_on_disk(root) -> str:
    """Every byte under the temp home, as text, so a test can say a name is
    nowhere."""
    chunks = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            try:
                chunks.append(path.read_bytes().decode("utf-8", "ignore"))
            except OSError:
                pass
    return "\n".join(chunks)


class TestAPrintNotKept:
    def test_the_start_says_once_what_is_kept(self, home):
        adapter = _printer()
        out = _start(adapter, keep_record=False)
        assert out["success"] is True, out
        assert out["print_kept"] == "none"
        assert json.dumps(out).count(print_keeping.NOT_KEPT_SAID) == 1
        assert adapter.sent == ["secret_part.gcode"]

    def test_only_the_safety_record_lives_while_it_prints(self, home):
        adapter = _printer()
        _start(adapter, keep_record=False)
        record = monitor_twin.active_twin("shop-x1")
        assert record is not None and record["kept"] == "none"
        assert record["file_name"] == "secret_part.gcode"
        assert _rows() == [], "no outcome row is opened for a print not kept"
        from kiln import daily_stats

        stats = daily_stats._read()
        assert stats.get("prints", 0) == 1, "counted, anonymously"
        assert not stats.get("pending_starts"), "no token naming the print"

    def test_nothing_of_it_is_left_once_it_ends(self, home):
        adapter = _printer()
        _start(adapter, keep_record=False)
        _end(adapter)
        assert monitor_twin.active_twin("shop-x1") is None
        assert not list(monitor_twin._TWIN_DIR.glob("shop-x1-*"))
        assert _rows() == []
        from kiln.persistence import get_db

        assert get_db().outcomes_owed_to_account() == []
        # The ending reported by hand afterwards is not recorded either.
        from kiln.plugins.learning_tools import record_print_outcome

        said = record_print_outcome(job_id="secret_part", outcome="success", printer_name="shop-x1")
        assert said["recorded"] is False and said["print_kept"] == "none"
        assert _rows() == []
        assert "secret_part" not in _everything_on_disk(home)

    def test_the_action_log_names_no_print(self, home):
        adapter = _printer()
        _start(adapter, keep_record=False)
        from kiln.persistence import get_db

        entries = get_db().query_audit(tool_name="start_print", limit=10)
        assert entries, "the action itself is still logged"
        assert "secret_part" not in json.dumps(entries, default=str)
        assert any("none" in json.dumps(e, default=str) for e in entries)

    def test_nothing_is_pooled(self, home, monkeypatch):
        from kiln import community_outbox

        adapter = _printer()
        _start(adapter, keep_record=False)
        sent: list[dict] = []
        monkeypatch.setattr(community_outbox, "contribute", lambda *a, **k: sent.append(k) or {"contributed": True})
        from kiln import community_autofire

        answer = community_autofire.auto_contribute_completion(
            outcome="success", printer_file_name="secret_part.gcode", printer_name="shop-x1",
        )
        assert answer["contributed"] is False
        assert sent == []

    def test_a_kept_print_still_records_the_way_it_always_did(self, home):
        adapter = _printer()
        out = _start(adapter)
        assert out["success"] is True
        assert out["print_kept"] == "kept"
        assert print_keeping.NOT_KEPT_SAID not in json.dumps(out)
        assert len(_rows()) == 1 and _rows()[0]["outcome"] == "pending"
        _end(adapter)
        assert monitor_twin.active_twin("shop-x1") is not None, "a kept print's record stays for the Monitor"
        assert [r["outcome"] for r in _rows()] != ["pending"]


class TestThisComputerOnly:
    def test_rows_stay_here_and_nothing_goes_up(self, home, monkeypatch):
        print_keeping.remember_account_choice(print_keeping.COMPUTER)
        adapter = _printer()
        out = _start(adapter, version_id="v1", design_id="d1")
        assert out["print_kept"] == "local"
        _end(adapter)
        rows = _rows()
        assert rows and all(r["kept"] == "local" for r in rows)
        from kiln.persistence import get_db

        assert get_db().outcomes_owed_to_account() == []
        from kiln import outcome_courier

        calls: list[Any] = []
        monkeypatch.setattr("kiln.history_courier._request", lambda *a, **k: calls.append(a) or {
            "status": "success", "print_history": "computer"})
        monkeypatch.setattr("kiln.auth_session.resolve_api_bearer", lambda: MagicMock(token="t"))
        answer = outcome_courier.push_owed(wait=1)
        assert answer["sent"] == 0
        assert all(c[2] != outcome_courier.PUSH_PATH for c in calls), "nothing pushed"

    def test_no_printer_check_in_goes_up(self, home, monkeypatch):
        print_keeping.remember_account_choice(print_keeping.COMPUTER)
        from kiln import _pro_printer_checkin_bridge as checkin

        monkeypatch.setattr("kiln.auth_session.resolve_api_bearer", lambda *a, **k: MagicMock(token="t"))
        assert checkin.ask_now("shop-x1") == {"asked": False, "why": "kept_on_this_computer"}

    def test_a_served_tool_is_sent_no_print_history(self, home):
        print_keeping.remember_account_choice(print_keeping.COMPUTER)
        from kiln import served_steps

        sent, refusal = served_steps._print_history("get_print_learning_summary", {})
        assert sent is None and refusal["code"] == "PRINT_HISTORY_KEPT_HERE"

    def test_cloud_sync_keeps_jobs_and_events_here(self, home):
        print_keeping.remember_account_choice(print_keeping.COMPUTER)
        from kiln.cloud_sync import CloudSyncManager, SyncConfig

        db = MagicMock()
        db.get_setting.return_value = "0"
        db.get_unsynced_jobs.return_value = [{"id": "j1"}]
        db.get_unsynced_events.return_value = [{"id": 1}]
        db.list_printers.return_value = []
        mgr = CloudSyncManager(db=db)
        mgr.configure(SyncConfig(cloud_url="https://example.invalid", api_key="k"))
        with patch.object(CloudSyncManager, "_push") as pushed:
            result = mgr.sync_now()
        assert result["jobs_pushed"] == 0 and result["events_pushed"] == 0
        assert all(c.args[0] not in ("jobs", "events") for c in pushed.call_args_list)

    def test_never_pooled(self, home):
        print_keeping.remember_account_choice(print_keeping.COMPUTER)
        from kiln import community_outbox

        answer = community_outbox.contribute_print_outcome(outcome="success", job_id="j", printer_file_name="a.gcode")
        assert answer["contributed"] is False


def _team_rule(monkeypatch, *, printers=("shop-x1",), designs=("team-design",)):
    from kiln import sharing_consent

    data = sharing_consent._read()
    data.update({
        print_keeping._TEAM_RULES_KEY: [{
            "org_id": "org-1", "name": "Acme", "printers": list(printers), "design_ids": list(designs),
        }],
        print_keeping._TEAM_RULES_READ_KEY: 9e12,
    })
    sharing_consent._write(data)


class TestATeamRule:
    def test_a_team_design_on_a_team_printer_is_held_before_anything_is_sent(self, home, monkeypatch):
        _team_rule(monkeypatch)
        adapter = _printer()
        out = _start(adapter, keep_record=False, design_id="team-design")
        assert out["success"] is False
        assert out.get("code") == print_keeping.TEAM_HOLD_CODE
        assert "keeps its prints" in json.dumps(out)
        assert adapter.sent == [], "nothing reached the printer"
        again = _start(adapter, keep_record=True, design_id="team-design")
        assert again["success"] is True and again["print_kept"] == "kept"
        assert adapter.sent == ["secret_part.gcode"]
        assert len(_rows()) == 1

    def test_a_personal_design_on_the_team_printer_stays_private(self, home, monkeypatch):
        _team_rule(monkeypatch)
        adapter = _printer()
        out = _start(adapter, keep_record=False, design_id="my-own-design")
        assert out["success"] is True and out["print_kept"] == "none"
        assert _rows() == []

    def test_a_team_design_on_a_personal_printer_follows_the_person(self, home, monkeypatch):
        _team_rule(monkeypatch, printers=("shop-other",))
        adapter = _printer()
        out = _start(adapter, keep_record=False, design_id="team-design")
        assert out["success"] is True and out["print_kept"] == "none"

    def test_this_computer_only_is_told_once_then_kept(self, home, monkeypatch):
        _team_rule(monkeypatch)
        print_keeping.remember_account_choice(print_keeping.COMPUTER)
        adapter = _printer()
        first = _start(adapter, design_id="team-design")
        assert first["success"] is False and first["code"] == print_keeping.TEAM_HOLD_CODE
        second = _start(adapter, design_id="team-design")
        assert second["success"] is True and second["print_kept"] == "kept"
        assert "keeps its prints" in second["kept_note"]


class TestTheRunningPrintsRecord:
    def test_the_ending_watcher_holds_a_print_not_kept_until_it_ends(self, home):
        from kiln import ending_watch

        adapter = _printer()
        _start(adapter, keep_record=False)
        assert ending_watch._pending_rows("shop-x1"), "the watcher stays to see it end"
        _end(adapter)
        assert ending_watch._pending_rows("shop-x1") == []

    def test_the_web_monitor_is_never_sent_its_toolpath(self, home):
        adapter = _printer()
        _start(adapter, keep_record=False)
        answer = monitor_twin.publish("shop-x1")
        assert answer["success"] is False and answer["code"] == "PRINT_NOT_SENT"

    def test_a_power_cut_does_not_end_it(self, home):
        """A printer that comes back idle after a power cut has not ended the
        print in Kiln's eyes: the record a resume reads stays until a real
        ending is seen or the next print starts."""
        adapter = _printer()
        _start(adapter, keep_record=False)
        adapter.state_word = "offline"
        adapter.get_state()  # the power goes
        adapter.state_word = "idle"
        adapter._base_outcomes_reconciled = False
        adapter.get_state()  # it comes back idle
        assert monitor_twin.active_twin("shop-x1") is not None
        _start(adapter)  # the next print replaces it
        assert monitor_twin.active_twin("shop-x1").get("kept") is None


class TestWhenSomethingGoesWrongTheAskStands:
    """A failure while deciding never turns a print the person asked not to
    keep into a kept one, and never sends what they keep on this computer."""

    def test_a_decision_that_raises_keeps_nothing_of_a_print_asked_not_to_be_kept(self, home, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("the team rules could not be read")

        monkeypatch.setattr(print_keeping, "_team_rule_at_start", _boom)
        adapter = _printer()
        out = _start(adapter, keep_record=False, design_id="team-design")
        assert out["success"] is True and out["print_kept"] == "none"
        assert out.get("kept_note") == print_keeping.NOT_KEPT_SAID
        _end(adapter)
        assert _rows() == []
        from kiln.persistence import get_db

        assert get_db().outcomes_owed_to_account() == []
        assert monitor_twin.active_twin("shop-x1") is None
        assert "secret_part" not in _everything_on_disk(home)

    def test_this_computer_only_never_becomes_the_account_on_a_failure(self, home, monkeypatch):
        print_keeping.remember_account_choice(print_keeping.COMPUTER)

        def _boom(*a, **k):
            raise RuntimeError("the team rules could not be read")

        monkeypatch.setattr(print_keeping, "_team_rule_at_start", _boom)
        out = _start(_printer())
        assert out["print_kept"] == "local"

    def test_an_unreadable_choice_keeps_history_on_this_computer(self, home):
        from kiln import sharing_consent

        path = sharing_consent.store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        assert print_keeping.account_choice() == print_keeping.COMPUTER
        assert print_keeping.may_leave() is False

    def test_an_unreadable_running_record_writes_nothing(self, home, monkeypatch):
        def _boom(*a, **k):
            raise OSError("the start record could not be read")

        monkeypatch.setattr(print_keeping, "_running", _boom)
        assert print_keeping.level_of("shop-x1", "secret_part.gcode") == print_keeping.NONE

    def test_a_lost_start_record_still_keeps_nothing_in_this_process(self, home, monkeypatch):
        adapter = _printer()
        monkeypatch.setattr(monitor_twin, "note_print_started", lambda *a, **k: None)
        _start(adapter, keep_record=False)
        _end(adapter)
        assert _rows() == []
