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
import pathlib
import time
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


def _printer(name: str = "shop-x1", job_file: str = "secret_part.gcode", *,
             serial: str | None = None, host: str | None = None, family: str | None = None):
    """A stand-in printer registered as *name*.  *serial* / *host* give it a
    machine identity; *family* is its backend's name (what a real adapter's
    ``name`` answers), so one machine can be registered under two names."""
    from kiln.printers.base import JobProgress, PrinterAdapter, PrinterState, PrinterStatus, PrintResult

    class _Fake(PrinterAdapter):
        _DURATION_SEMANTICS = "frozen"

        def __init__(self):
            self.state_word = "idle"
            self.sent: list[str] = []
            self._kiln_registered_name = name
            if serial:
                self._serial = serial
            if host:
                self._host = host

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

    _Fake.name = family or name
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


#: The shop's printer: one machine, known by its serial number.
SHOP_SERIAL = "01P00A391800231"
TEAM_KEY = "a" * 64
OTHER_TEAM_KEY = "b" * 64


def _shop_printer(name: str = "shop-x1", serial: str = SHOP_SERIAL):
    return _printer(name, serial=serial, family="bambu")


def _team_id(adapter, key: str = TEAM_KEY) -> str:
    from kiln.outbound_guard import team_printer_id
    from kiln.registry import stable_machine_fingerprint

    return team_printer_id(stable_machine_fingerprint(adapter), key)


def _team_rule(monkeypatch, *, machines=None, designs=("team-design",), key: str = TEAM_KEY,
               required: bool = True, org_id: str = "org-1", name: str = "Acme", also=()):
    """A team rule as the account serves it, read on this computer just now:
    *machines* (stand-in printers) are the team's list, by their team ids."""
    from kiln import sharing_consent

    machines = [_shop_printer()] if machines is None else machines
    data = sharing_consent._read()
    rules = [*also, {
        "org_id": org_id, "name": name, "required": required, "team_key": key,
        "printers": [{"id": _team_id(m, key), "label": "Shop X1", "model": "X1 Carbon", "known_by": "serial"}
                     for m in machines],
        "design_ids": list(designs),
    }]
    data.update({print_keeping._TEAM_RULES_KEY: rules, print_keeping._TEAM_RULES_READ_KEY: time.time()})
    sharing_consent._write(data)
    return rules


class TestATeamRule:
    def test_a_team_design_on_a_team_printer_is_held_before_anything_is_sent(self, home, monkeypatch):
        _team_rule(monkeypatch)
        adapter = _shop_printer()
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
        adapter = _shop_printer()
        out = _start(adapter, keep_record=False, design_id="my-own-design")
        assert out["success"] is True and out["print_kept"] == "none"
        assert _rows() == []

    def test_a_team_design_on_a_personal_printer_follows_the_person(self, home, monkeypatch):
        _team_rule(monkeypatch)
        adapter = _shop_printer(serial="01P00A000000999")
        out = _start(adapter, keep_record=False, design_id="team-design")
        assert out["success"] is True and out["print_kept"] == "none"

    def test_this_computer_only_is_told_once_then_kept(self, home, monkeypatch):
        _team_rule(monkeypatch)
        print_keeping.remember_account_choice(print_keeping.COMPUTER)
        adapter = _shop_printer()
        first = _start(adapter, design_id="team-design")
        assert first["success"] is False and first["code"] == print_keeping.TEAM_HOLD_CODE
        second = _start(adapter, design_id="team-design")
        assert second["success"] is True and second["print_kept"] == "kept"
        assert "keeps its prints" in second["kept_note"]


class TestATeamPrinterIsTheMachine:
    """The team's list names machines.  A member who calls the shop printer
    something else, or renames it, prints on the same team printer."""

    def test_the_same_machine_under_another_name_is_still_the_teams(self, home, monkeypatch):
        _team_rule(monkeypatch, machines=[_shop_printer("Shop X1")])
        mine = _shop_printer("my bambu")
        out = _start(mine, keep_record=False, design_id="team-design")
        assert out["success"] is False and out.get("code") == print_keeping.TEAM_HOLD_CODE
        assert mine.sent == []

    def test_a_printer_named_like_the_team_printer_is_not_it(self, home, monkeypatch):
        _team_rule(monkeypatch, machines=[_shop_printer("Shop X1")])
        lookalike = _shop_printer("Shop X1", serial="01P00A000000777")
        out = _start(lookalike, keep_record=False, design_id="team-design")
        assert out["success"] is True and out["print_kept"] == "none"

    def test_a_printer_known_by_its_address_is_matched_by_it(self, home, monkeypatch):
        added = _printer("klipper", host="http://192.168.1.40:7125/", family="moonraker")
        _team_rule(monkeypatch, machines=[added])
        here = _printer("voron", host="192.168.1.40:7125", family="moonraker")
        out = _start(here, keep_record=False, design_id="team-design")
        assert out.get("code") == print_keeping.TEAM_HOLD_CODE

    def test_a_printer_whose_identity_cannot_be_read_is_no_teams_printer(self, home, monkeypatch):
        _team_rule(monkeypatch)
        nameless = _printer("shop-x1")  # no serial, no address
        out = _start(nameless, keep_record=False, design_id="team-design")
        assert out["success"] is True and out["print_kept"] == "none"
        assert print_keeping.team_rule_for(nameless, "team-design") is None

    def test_one_teams_list_says_nothing_about_another_team(self, home, monkeypatch):
        # Team B lists ids made under team A's key: they name no machine of B's.
        shop = _shop_printer()
        rules = _team_rule(monkeypatch, machines=[shop], key=TEAM_KEY)
        rules[0]["team_key"] = OTHER_TEAM_KEY
        from kiln import sharing_consent

        data = sharing_consent._read()
        data[print_keeping._TEAM_RULES_KEY] = rules
        sharing_consent._write(data)
        out = _start(shop, keep_record=False, design_id="team-design")
        assert out["success"] is True and out["print_kept"] == "none"

    def test_a_team_whose_owner_requires_nothing_keeps_nothing(self, home, monkeypatch):
        _team_rule(monkeypatch, required=False)
        out = _start(_shop_printer(), keep_record=False, design_id="team-design")
        assert out["success"] is True and out["print_kept"] == "none"


class TestTheTeamPrinterId:
    def test_one_machine_one_id_per_team(self):
        from kiln.outbound_guard import is_team_printer_id, team_printer_id

        fp = "bambu:serial:01p00a391800231"
        first, again = team_printer_id(fp, TEAM_KEY), team_printer_id(fp.upper(), TEAM_KEY)
        assert first == again and is_team_printer_id(first)
        assert team_printer_id(fp, OTHER_TEAM_KEY) != first, "another team, another id"
        assert team_printer_id("bambu:serial:01p00a000000001", TEAM_KEY) != first
        assert "01p00a391800231" not in first
        assert team_printer_id("", TEAM_KEY) is None and team_printer_id(fp, "") is None


def _account(monkeypatch, *, rules, answers=None):
    """The account behind ``history_courier._request``: the team rules for a
    GET, *answers* (or a success) for a PATCH.  Returns what was sent."""
    sent: list[tuple[str, str, Any]] = []

    def fake(tool, method, path, body=None, **kw):
        sent.append((method, path, body))
        if method == "GET":
            return {"status": "success", "rules": rules}
        return (answers or {}).get(path, {"success": True})

    monkeypatch.setattr("kiln.history_courier._request", fake)
    return sent


class TestAddingATeamPrinter:
    def _rules(self, *teams):
        return [{"org_id": org, "name": name, "required": True, "team_key": key, "printers": [], "design_ids": []}
                for org, name, key in teams or [("org-1", "Acme", TEAM_KEY)]]

    def test_the_machine_goes_as_its_team_id_and_never_as_its_serial(self, home, monkeypatch):
        sent = _account(monkeypatch, rules=self._rules())
        shop = _shop_printer("Shop X1")
        out = print_keeping.add_team_printer(shop, "Shop X1")
        assert out["success"] is True, out
        method, path, body = sent[1]
        assert (method, path) == ("PATCH", "/api/team/orgs/org-1/print-history")
        assert body == {"add_printer": {"id": _team_id(shop), "known_by": "serial", "label": "Shop X1"}}
        assert SHOP_SERIAL.lower() not in json.dumps(body).lower()

    def test_a_name_holding_the_serial_is_left_out(self, home, monkeypatch):
        sent = _account(monkeypatch, rules=self._rules())
        shop = _shop_printer(f"x1 {SHOP_SERIAL}")
        out = print_keeping.add_team_printer(shop, f"x1 {SHOP_SERIAL}")
        assert out["success"] is True
        assert "label" not in sent[1][2]["add_printer"]
        assert SHOP_SERIAL not in json.dumps(sent)
        refused = print_keeping.add_team_printer(shop, "x1", label=f"shop {SHOP_SERIAL}")
        assert refused["success"] is False and refused["code"] == "LABEL_NOT_SENT"

    def test_a_printer_with_no_identity_is_refused_and_nothing_is_asked(self, home, monkeypatch):
        sent = _account(monkeypatch, rules=self._rules())
        out = print_keeping.add_team_printer(_printer("usb"), "usb")
        assert out["success"] is False and out["error"] == print_keeping.NO_IDENTITY_SAID
        assert sent == []

    def test_a_printer_known_by_its_address_is_said_so(self, home, monkeypatch):
        sent = _account(monkeypatch, rules=self._rules())
        out = print_keeping.add_team_printer(_printer("voron", host="192.168.1.40", family="moonraker"), "voron")
        assert out["success"] is True
        assert sent[1][2]["add_printer"]["known_by"] == "address"
        assert "192.168.1.40" not in json.dumps(sent)
        assert print_keeping.KNOWN_BY_ADDRESS_SAID in out["summary"]

    def test_two_teams_mean_saying_which(self, home, monkeypatch):
        _account(monkeypatch, rules=self._rules(("org-1", "Acme", TEAM_KEY), ("org-2", "Lab", OTHER_TEAM_KEY)))
        out = print_keeping.add_team_printer(_shop_printer(), "Shop X1")
        assert out["success"] is False and out["code"] == "WHICH_TEAM"
        sent = _account(monkeypatch, rules=self._rules(("org-1", "Acme", TEAM_KEY), ("org-2", "Lab", OTHER_TEAM_KEY)))
        shop = _shop_printer()
        assert print_keeping.add_team_printer(shop, "Shop X1", team="lab")["success"] is True
        assert sent[1][1] == "/api/team/orgs/org-2/print-history"
        assert sent[1][2]["add_printer"]["id"] == _team_id(shop, OTHER_TEAM_KEY)

    def test_the_accounts_refusal_is_said_in_its_own_words(self, home, monkeypatch):
        refusal = {"status": "error", "success": False, "error": "invalid_rule",
                   "detail": "The printer name is longer than 60 characters."}
        _account(monkeypatch, rules=self._rules(), answers={"/api/team/orgs/org-1/print-history": refusal})
        out = print_keeping.add_team_printer(_shop_printer(), "Shop X1")
        assert out["success"] is False and out["error"] == refusal["detail"]


class TestTakingATeamPrinterOff:
    def _rules(self, printers):
        return [{"org_id": "org-1", "name": "Acme", "required": True, "team_key": TEAM_KEY,
                 "printers": printers, "design_ids": []}]

    def test_it_is_named_by_the_name_the_list_shows(self, home, monkeypatch):
        listed = {"id": _team_id(_shop_printer()), "label": "Shop X1", "model": "", "known_by": "serial"}
        sent = _account(monkeypatch, rules=self._rules([listed]))
        out = print_keeping.remove_team_printer("shop x1")
        assert out["success"] is True
        assert sent[1][2] == {"remove_printer": listed["id"]}

    def test_two_of_one_name_mean_saying_which(self, home, monkeypatch):
        one = {"id": _team_id(_shop_printer()), "label": "X1", "model": "", "known_by": "serial"}
        two = {"id": _team_id(_shop_printer(serial="01P00A000000555")), "label": "X1", "model": "", "known_by": "serial"}
        sent = _account(monkeypatch, rules=self._rules([one, two]))
        out = print_keeping.remove_team_printer("X1")
        assert out["success"] is False and out["code"] == "WHICH_TEAM_PRINTER"
        assert [s for s in sent if s[0] == "PATCH"] == []
        assert print_keeping.remove_team_printer(two["id"])["success"] is True

    def test_only_the_owner_is_the_accounts_to_say(self, home, monkeypatch):
        listed = {"id": _team_id(_shop_printer()), "label": "Shop X1", "model": "", "known_by": "serial"}
        refusal = {"status": "error", "success": False, "error": "forbidden",
                   "detail": "Only the team's owner can take a printer off the team's list."}
        _account(monkeypatch, rules=self._rules([listed]), answers={"/api/team/orgs/org-1/print-history": refusal})
        out = print_keeping.remove_team_printer("Shop X1")
        assert out["success"] is False and out["error"] == refusal["detail"]


class TestTheListing:
    def test_a_printer_set_up_here_is_marked_with_its_name_here(self, home, monkeypatch):
        shop = _shop_printer("my bambu")
        listed = {"id": _team_id(shop), "label": "Shop X1", "model": "", "known_by": "serial"}
        _account(monkeypatch, rules=[{"org_id": "org-1", "name": "Acme", "required": True,
                                      "team_key": TEAM_KEY, "printers": [listed], "design_ids": []}])
        registry = MagicMock()
        registry.list_all.return_value = {"my bambu": shop, "other": _shop_printer(serial="01P00A000000444")}
        out = print_keeping.team_printer_listing(registry=registry)
        assert out["teams"][0]["printers"][0]["here"] == "my bambu"


class TestTheTerminalDoor:
    """``kiln sharing team-printers``: the person's own saved printer is put
    on the team's list as its team id, and the account's refusals are said."""

    def _saved(self, home):
        import yaml

        path = home / ".kiln" / "config.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump({"active_printer": "shop", "printers": {"shop": {
            "type": "bambu", "host": "192.168.1.77", "access_code": "12345678", "serial": SHOP_SERIAL,
        }}}), encoding="utf-8")

    def _cli(self, *args):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        return CliRunner().invoke(cli, ["sharing", "team-printers", *args])

    def test_add_sends_the_saved_printers_team_id(self, home, monkeypatch):
        self._saved(home)
        sent = _account(monkeypatch, rules=[{"org_id": "org-1", "name": "Acme", "required": True,
                                             "team_key": TEAM_KEY, "printers": [], "design_ids": []}])
        out = self._cli("add")
        assert out.exit_code == 0, out.output
        assert "is now one of Acme's printers" in out.output
        body = sent[1][2]["add_printer"]
        assert body["label"] == "shop" and body["known_by"] == "serial"
        from kiln.outbound_guard import team_printer_id

        assert body["id"] == team_printer_id(f"bambu:serial:{SHOP_SERIAL.lower()}", TEAM_KEY)
        for private in (SHOP_SERIAL, SHOP_SERIAL.lower(), "192.168.1.77", "12345678"):
            assert private not in json.dumps(sent)

    def test_a_refusal_is_said_and_exits_nonzero(self, home, monkeypatch):
        self._saved(home)
        refusal = {"status": "error", "success": False, "error": "forbidden",
                   "detail": "Only the team's owner can take a printer off the team's list."}
        listed = {"id": "tp:" + "9" * 32, "label": "shop", "model": "", "known_by": "serial"}
        _account(monkeypatch, rules=[{"org_id": "org-1", "name": "Acme", "required": True, "team_key": TEAM_KEY,
                                      "printers": [listed], "design_ids": []}],
                 answers={"/api/team/orgs/org-1/print-history": refusal})
        out = self._cli("remove", "shop")
        assert out.exit_code == 1 and refusal["detail"] in out.output


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


#: Every tool a person can start or queue a print through.
PRINT_DOORS = frozenset({
    "start_print", "slice_and_print", "start_monitored_print", "download_and_upload",
    "print_plate_object", "run_quick_print", "generate_and_print", "retry_print_with_fix",
    "submit_job",
})


class TestEveryDoor:
    def test_every_print_door_takes_the_one_switch(self):
        import kiln.server as srv

        tools = srv.mcp._tool_manager._tools
        missing = sorted(n for n in PRINT_DOORS if "keep_record" not in (tools[n].parameters.get("properties") or {}))
        assert missing == [], f"print doors without keep_record: {missing}"

    def test_a_new_print_door_is_on_the_list(self):
        """The doors that start or queue a print, read from the source
        (kiln.print_doors): a registered tool among them is on PRINT_DOORS."""
        import kiln.server as srv
        from kiln.print_doors import enumerate_print_doors

        tools = set(srv.mcp._tool_manager._tools)
        doors = {d.function for d in enumerate_print_doors() if d.kind in ("start", "queue")}
        unlisted = sorted((doors & tools) - PRINT_DOORS)
        assert unlisted == [], f"a print door with no switch: {unlisted}"

    def test_the_cli_print_command_takes_it(self):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        out = CliRunner().invoke(cli, ["print", "--help"])
        assert "--dont-keep" in out.output

    def test_never_suggested_in_any_door_description(self):
        import kiln.server as srv

        tools = srv.mcp._tool_manager._tools
        for name in PRINT_DOORS:
            said = " ".join(str(tools[name].description or "").split())
            assert "only when the person asks" in said and "never suggest it" in said, name


class TestAQueuedPrintNotKept:
    def test_the_queue_forgets_it_when_it_ends(self, home):
        import sqlite3

        from kiln.queue import PrintQueue

        db = home / "queue.db"
        queue = PrintQueue(db_path=str(db))
        job_id = queue.submit("secret_part.gcode", metadata={"keep_record": False, "design_id": "d-secret"})
        queue.mark_starting(job_id, printer_name="shop-x1")
        queue.mark_printing(job_id)
        queue.mark_completed(job_id)
        job = queue.get_job(job_id)
        assert job.file_name == "" and "design_id" not in job.metadata
        rows = sqlite3.connect(str(db)).execute("SELECT id FROM jobs WHERE id=?", (job_id,)).fetchall()
        assert rows == []

    def test_a_kept_queued_job_stays(self, home):
        from kiln.queue import PrintQueue

        queue = PrintQueue(db_path=str(home / "queue.db"))
        job_id = queue.submit("part.gcode")
        queue.mark_starting(job_id, printer_name="shop-x1")
        queue.mark_printing(job_id)
        queue.mark_completed(job_id)
        assert queue.get_job(job_id).file_name == "part.gcode"


class TestNothingNamesItAfterItEnds:
    def test_the_slice_records_and_the_plate_forget_its_name(self, home, monkeypatch):
        import os

        from kiln import plate_state, print_lineage

        model = home / "secret_part.stl"
        model.write_text("solid x\nendsolid x\n")
        gcode = home / "secret_part.gcode"
        gcode.write_text("G28\n")
        monitor_twin.note_sliced(str(model), str(gcode))
        monkeypatch.setattr(print_lineage, "_hosted", lambda: False)
        print_lineage.record_slice(str(model), str(gcode))
        print_lineage.flush() if hasattr(print_lineage, "flush") else None
        monkeypatch.setattr(plate_state, "machine_id", lambda adapter: "machine-1")
        adapter = _printer()
        _start(adapter, keep_record=False)
        store = plate_state._read_store()
        assert "secret_part" in json.dumps(store), "the plate holds the part while it prints"
        _end(adapter)
        text = "".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in (plate_state._store_path(), print_lineage.store_path(), monitor_twin._SLICES_FILE)
            if path.exists()
        )
        assert "secret_part" not in text
        row = plate_state._read_store()["machines"]["machine-1"]
        assert row["status"] == "occupied", "something is still on the plate"
        assert os.path.exists(gcode), "the person's own file is theirs"


class TestATeamRuleThatCannotBeRead:
    """A not-kept print of a team's design on the team's printer waits while
    the team's rule cannot be read; the person's own designs do not."""

    def _stale_rule(self, monkeypatch):
        _team_rule(monkeypatch)
        from kiln import sharing_consent

        data = sharing_consent._read()
        data[print_keeping._TEAM_RULES_READ_KEY] = time.time() - 3600  # an old reading
        sharing_consent._write(data)
        monkeypatch.setattr("kiln.history_courier._request", lambda *a, **k: {"status": "error", "why": "offline"})

    def test_a_team_design_on_the_team_printer_is_held_and_nothing_is_sent(self, home, monkeypatch):
        self._stale_rule(monkeypatch)
        adapter = _shop_printer()
        out = _start(adapter, keep_record=False, design_id="team-design")
        assert out["success"] is False and out.get("code") == print_keeping.TEAM_HOLD_CODE
        assert print_keeping.TEAM_RULE_UNREAD_SAID in json.dumps(out)
        assert adapter.sent == []
        kept = _start(adapter, keep_record=True, design_id="team-design")
        assert kept["success"] is True and kept["print_kept"] == "kept"

    def test_a_personal_design_in_the_same_state_prints_privately(self, home, monkeypatch):
        self._stale_rule(monkeypatch)
        adapter = _shop_printer()
        out = _start(adapter, keep_record=False, design_id="my-own-design")
        assert out["success"] is True and out["print_kept"] == "none"
        assert adapter.sent == ["secret_part.gcode"]


class TestThePlateCamera:
    """A picture of the plate while it holds a print not kept is a record of
    the print: none is kept, none is linked to the plate's record, and every
    one is deleted when the print ends."""

    def test_no_picture_outlives_the_print(self, home, monkeypatch):
        import base64

        from kiln import plate_state

        monkeypatch.setattr(plate_state, "machine_id", lambda adapter: "machine-1")
        adapter = _printer()
        _start(adapter, keep_record=False)
        found = plate_state.PlateLook(available=True, camera="printer",
                                      image_b64=base64.b64encode(b"\x89PNG-secret").decode(), media_type="image/png")
        path = plate_state.hand_over_frame(adapter, found)
        assert path and pathlib.Path(path).exists(), "eyes still get the picture while it prints"
        assert machine_not_in(plate_state._read_store().get("frames"), "machine-1")
        assert plate_state._keep_judged_frame(adapter) is None
        assert not (pathlib.Path(plate_state._kiln_dir()) / plate_state._LOOKS_DIR).exists()
        _end(adapter)
        assert not pathlib.Path(path).exists()

    def test_a_kept_print_keeps_its_judged_picture(self, home, monkeypatch):
        import base64

        from kiln import plate_state

        monkeypatch.setattr(plate_state, "machine_id", lambda adapter: "machine-1")
        adapter = _printer()
        _start(adapter)
        found = plate_state.PlateLook(available=True, camera="printer",
                                      image_b64=base64.b64encode(b"\x89PNG").decode(), media_type="image/png")
        plate_state.hand_over_frame(adapter, found)
        assert plate_state._keep_judged_frame(adapter) is not None


def machine_not_in(frames, machine):
    return not isinstance(frames, dict) or machine not in frames


class TestWebhooks:
    def _manager(self):
        from kiln.events import EventBus
        from kiln.webhooks import WebhookManager

        mgr = WebhookManager(EventBus())
        mgr.register("https://example.com/hook")
        return mgr

    def test_a_print_not_kept_sends_no_event(self, home):
        from kiln.events import Event, EventType

        adapter = _printer()
        _start(adapter, keep_record=False)
        mgr = self._manager()
        mgr._on_event(Event(type=EventType.PRINT_COMPLETED,
                            data={"printer_name": "shop-x1", "file_name": "secret_part.gcode"}))
        mgr._on_event(Event(type=EventType.PRINT_PROGRESS, data={"printer_name": "shop-x1", "progress": 40}))
        assert mgr._delivery_queue.empty()

    def test_a_safety_event_still_reaches_the_person_without_the_name(self, home):
        from kiln.events import Event, EventType

        adapter = _printer()
        _start(adapter, keep_record=False)
        mgr = self._manager()
        mgr._on_event(Event(type=EventType.TEMPERATURE_WARNING,
                            data={"printer_name": "shop-x1", "file_name": "secret_part.gcode", "tool_c": 290}))
        _endpoint, event = mgr._delivery_queue.get_nowait()
        assert "secret_part" not in json.dumps(event.data) and event.data["tool_c"] == 290

    def test_a_kept_print_still_sends_its_events(self, home):
        from kiln.events import Event, EventType

        adapter = _printer()
        _start(adapter)
        mgr = self._manager()
        mgr._on_event(Event(type=EventType.PRINT_COMPLETED,
                            data={"printer_name": "shop-x1", "file_name": "secret_part.gcode"}))
        assert not mgr._delivery_queue.empty()


class TestARunOfCopiesNotKept:
    def _plan(self, plan_id):
        import time as _t

        from kiln.persistence import get_db

        db = get_db()
        db._conn.execute(
            "INSERT INTO split_plans (id, original_file, split_type, parts, created_at, status) VALUES (?,?,?,?,?,?)",
            (plan_id, "/tmp/secret_part.stl", "fill", json.dumps({"fill": {}}), _t.time(), "submitted"),
        )
        db._conn.commit()

    def _rows(self, plan_id):
        from kiln.persistence import get_db

        return get_db()._conn.execute("SELECT id FROM split_plans WHERE id=?", (plan_id,)).fetchall()

    def test_the_run_is_forgotten_only_once_all_of_it_has_ended(self, home):
        import sqlite3

        from kiln.queue import PrintQueue

        part = home / ".kiln" / "queued_parts" / "part-secret.stl"
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_text("solid x")
        self._plan("plan-1")
        queue = PrintQueue(db_path=str(home / "queue.db"))
        meta = {"keep_record": False, "split_plan_id": "plan-1",
                "fill": {"model_path": str(part), "owned_copy": str(part), "copies": 2}}
        first = queue.submit("secret_part.stl", metadata=dict(meta))
        second = queue.submit("secret_part.stl", metadata=dict(meta))
        for job_id in (first,):
            queue.mark_starting(job_id, printer_name="shop-x1")
            queue.mark_printing(job_id)
            queue.mark_completed(job_id)
        assert queue.get_job(first).file_name == "secret_part.stl", "the run is not over"
        assert self._rows("plan-1") and part.exists()
        queue.mark_starting(second, printer_name="shop-x1")
        queue.mark_printing(second)
        queue.mark_completed(second)
        assert queue.get_job(first).file_name == "" and queue.get_job(second).file_name == ""
        assert "model_path" not in queue.get_job(second).metadata.get("fill", {})
        assert not part.exists()
        assert self._rows("plan-1") == []
        assert sqlite3.connect(str(home / "queue.db")).execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0

    def test_a_failed_plate_waits_for_the_queue_to_settle_it(self, home):
        from kiln.queue import PrintQueue

        self._plan("plan-2")
        queue = PrintQueue(db_path=str(home / "queue.db"))
        job_id = queue.submit("secret_part.stl", metadata={"keep_record": False, "split_plan_id": "plan-2",
                                                            "fill": {"model_path": "/nowhere.stl", "copies": 1}})
        queue.mark_starting(job_id, printer_name="shop-x1")
        queue.mark_printing(job_id)
        queue.mark_failed(job_id, "spaghetti")
        assert queue.get_job(job_id).file_name == "secret_part.stl", "it may be printed again"
        assert queue.forget_run_if_ended(queue.get_job(job_id)) is True
        assert self._rows("plan-2") == []

    def test_the_fill_doors_take_the_switch(self):
        import kiln.server as srv

        tools = srv.mcp._tool_manager._tools
        for name in ("multi_copy_print", "plan_multi_copy_split"):
            assert "keep_record" in (tools[name].parameters.get("properties") or {}), name


class TestOnlyWhatKilnMadeIsDeleted:
    def test_the_persons_own_model_survives_and_kilns_copy_goes(self, home):
        from kiln.queue import PrintQueue

        own = home / "Desktop" / "secret_part.stl"
        own.parent.mkdir(parents=True, exist_ok=True)
        own.write_text("solid mine")
        copy = home / ".kiln" / "queued_parts" / "part-abc.stl"
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_text("solid mine")
        queue = PrintQueue(db_path=str(home / "queue.db"))
        # A job whose part is the person's own file (Kiln made no copy), and
        # one carrying Kiln's own copy, as recorded when it was made.
        mine = queue.submit("secret_part.stl", metadata={"keep_record": False,
                                                          "fill": {"model_path": str(own), "copies": 1}})
        kilns = queue.submit("secret_part.stl", metadata={"keep_record": False, "fill": {
            "model_path": str(copy), "owned_copy": str(copy), "copies": 1}})
        for job_id in (mine, kilns):
            queue.mark_starting(job_id, printer_name="shop-x1")
            queue.mark_printing(job_id)
            queue.mark_completed(job_id)
        assert own.exists(), "the person's own file is theirs"
        assert not copy.exists()

    def test_a_copy_named_outside_kilns_folder_is_never_deleted(self, home):
        from kiln.queue import PrintQueue

        own = home / "Desktop" / "keep_me.stl"
        own.parent.mkdir(parents=True, exist_ok=True)
        own.write_text("solid mine")
        queue = PrintQueue(db_path=str(home / "queue.db"))
        job_id = queue.submit("keep_me.stl", metadata={"keep_record": False, "fill": {
            "model_path": str(own), "owned_copy": str(own), "failed_picture": str(own), "copies": 1}})
        queue.mark_starting(job_id, printer_name="shop-x1")
        queue.mark_printing(job_id)
        queue.mark_completed(job_id)
        assert own.exists()

    def test_the_twin_cleanup_touches_nothing_outside_its_folder(self, home):
        own = home / "Desktop" / "secret_part.gcode"
        own.parent.mkdir(parents=True, exist_ok=True)
        own.write_text("G28")
        monitor_twin._write_json(monitor_twin._ACTIVE_FILE, {"shop-x1": {
            "file_name": "secret_part.gcode", "printer_name": "shop-x1", "kept": "none", "gcode": str(own)}})
        assert monitor_twin.forget_print("shop-x1") is True
        assert own.exists()


class TestTheChoiceIsSaidInOneVoice:
    def test_setting_it_tells_an_agent_where_history_is_kept(self, home, monkeypatch):
        from kiln import print_keeping

        monkeypatch.setattr("kiln.history_courier._request", lambda *a, **k: {"status": "success"})
        said = print_keeping.set_account_choice(print_keeping.COMPUTER)
        assert said["summary"] == print_keeping.SAID_BY_THE_ACCOUNT[print_keeping.COMPUTER]
        assert said["summary"].startswith("Print history: kept only on the computer that printed it.")

    def test_the_terminal_says_the_last_setting_when_the_account_cannot_be_asked(self, home, monkeypatch):
        from click.testing import CliRunner

        from kiln import print_keeping
        from kiln.cli.main import cli

        monkeypatch.setattr(print_keeping, "refresh_account_choice", lambda **k: None)
        monkeypatch.setattr(print_keeping, "account_choice", lambda: print_keeping.ACCOUNT)
        out = CliRunner().invoke(cli, ["sharing", "history"]).output
        assert "Print history: kept in your Kiln account." in out
        assert print_keeping.LAST_SEEN in out
