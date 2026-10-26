"""A printer's repair marks follow the printer to the person's account, and back.

Pinned, each against the real ``KilnDB`` and the real engine:

* the account id a signed-in computer knows is the one the server keys the
  account on (``sub`` of a session or machine credential, ``jti`` of a
  license key) and nothing weaker -- signed out, or a bearer with no claim,
  is ``None``;
* the shared code for a printer whose own secret both computers hold is the
  same on two computers of one account, different for another account that
  holds the same printer, different after the access code changes, and
  nothing for a printer with no such secret or nobody signed in;
* a mark goes up once with only the named fields -- never the note, the
  printer's name, its serial or its address -- beside one announcement per
  printer; the account's marks for this computer's printers come down into
  this computer's record under the ids they carry, so none is taken twice,
  and this computer's own mark coming back is itself; the account's
  questions are kept for the printer they are about;
* a ``clear`` takes back the latest mark written before it, so a repair
  dated back to last week and recorded after a clear still stands;
* the three doors to an answer -- the tool, the command, the host's own
  dialog -- reach the one account call, and a call that is itself an
  answer is never asked;
* signed out, nothing goes up and nothing is asked; on the hosted server
  the courier is off.

A/B: every test here fails at public 9829032a (no courier, no shared code,
no account id, no ``same_as``).
"""

from __future__ import annotations

import base64
import json
import time
from types import SimpleNamespace

import pytest

from kiln import auth_session, outbound_guard
from kiln import machine_repair_courier as courier
from kiln import printer_repairs as pr
from kiln.persistence import KilnDB
from kiln.registry import machine_fingerprint

DAY = 86400.0


def _machine(name: str, *, access_code: str = "s3cret-access-code", serial: str | None = None):
    return SimpleNamespace(
        name=name, serial=serial or f"01P00A0000000{name}", host="192.168.7.42", access_code=access_code,
        _printer_model="bambu_a1", _kiln_registered_name=name,
        get_state=lambda: SimpleNamespace(state=SimpleNamespace(value="idle")),
    )


class _Registry:
    def __init__(self, machines):
        self.adapters = machines

    def list_names(self):
        return list(self.adapters)

    def list_all(self):
        return dict(self.adapters)

    def get(self, name):
        if name not in self.adapters:
            from kiln.registry import PrinterNotFoundError

            raise PrinterNotFoundError(name)
        return self.adapters[name]


def _jwt(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{body}.sig"


@pytest.fixture
def db(tmp_path):
    instance = KilnDB(str(tmp_path / "kiln.db"))
    yield instance
    instance.close()


@pytest.fixture(autouse=True)
def _own_install(monkeypatch):
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    monkeypatch.setenv(courier.TEST_OPT_IN_ENV, "1")
    pr._shared_codes.clear()
    courier._questions.clear()


@pytest.fixture
def signed_in(monkeypatch):
    token = _jwt({"sub": "acct-alice", "exp": time.time() + 3600})
    monkeypatch.setattr(auth_session, "resolve_api_bearer",
                        lambda *a, **k: auth_session.ApiBearer(token=token, state="session"))
    return "acct-alice"


@pytest.fixture
def registry(monkeypatch):
    a, b = _machine("a"), _machine("b")
    reg = _Registry({"a": a, "b": b})
    import kiln.server as srv

    monkeypatch.setattr(srv, "_get_registry", lambda: reg)
    return reg


@pytest.fixture
def account(monkeypatch):
    """Every call the courier makes, answered as the server would: marks
    kept, this computer's printers echoed with the marks the account holds
    for them (``held``), and the questions it has (``asks``)."""
    calls: list[dict] = []
    state = {"held": {}, "asks": [], "linked": True}

    def fake_request(tool, method, path, body=None, **kw):
        calls.append({"tool": tool, "method": method, "path": path, "body": body})
        if path == courier.ANSWER_PATH:
            return {"status": "success", "success": True, "linked": state["linked"]}
        printers = {}
        for p in (body or {}).get("machines") or []:
            printers[p["machine_code"]] = {"machine_key": "pk:" + p["machine_code"][3:],
                                           "marks": state["held"].get(p["machine_code"], [])}
        return {"status": "success", "success": True,
                "kept": [m["source_id"] for m in (body or {}).get("marks") or []], "refused": 0,
                "machines": printers, "questions": list(state["asks"])}

    import kiln.history_courier as hc

    monkeypatch.setattr(hc, "_request", fake_request)
    state["calls"] = calls
    return state


# ---------------------------------------------------------------------------
# The account id, and the shared code
# ---------------------------------------------------------------------------


class TestTheAccountId:
    def test_a_session_or_machine_credential_gives_its_sub(self, monkeypatch):
        token = _jwt({"sub": "uid-123", "exp": 9e9})
        monkeypatch.setattr(auth_session, "resolve_api_bearer",
                            lambda *a, **k: auth_session.ApiBearer(token=token, state="session"))
        assert auth_session.signed_in_account_id() == "uid-123"

    def test_a_license_key_gives_its_jti_never_its_sub(self, monkeypatch):
        token = _jwt({"sub": "someone", "jti": "lic-9", "exp": 9e9})
        monkeypatch.setattr(auth_session, "resolve_api_bearer",
                            lambda *a, **k: auth_session.ApiBearer(token=token, state="license"))
        assert auth_session.signed_in_account_id() == "lic-9"

    @pytest.mark.parametrize("bearer", [
        auth_session.ApiBearer(token="", state="signed_out"),
        auth_session.ApiBearer(token="not-a-jwt", state="session"),
        auth_session.ApiBearer(token=_jwt({"exp": 9e9}), state="session"),
    ])
    def test_nothing_weaker_than_an_account_id_is_ever_answered(self, monkeypatch, bearer):
        monkeypatch.setattr(auth_session, "resolve_api_bearer", lambda *a, **k: bearer)
        assert auth_session.signed_in_account_id() is None


class TestTheSharedCode:
    def test_two_computers_of_one_account_make_one_code_and_the_server_key_plays_no_part(self, monkeypatch):
        a = _machine("a")
        here = pr.shared_code(a, account_id="acct-1")
        pr._shared_codes.clear()
        monkeypatch.setattr(outbound_guard, "_code_key", b"another computer's secret")
        there = pr.shared_code(_machine("a"), account_id="acct-1")
        assert here == there
        assert here.startswith("pc:") and len(here) == 27 and int(here[3:], 16) >= 0

    def test_the_same_printer_in_another_account_is_another_code(self):
        a = _machine("a")
        assert pr.shared_code(a, account_id="acct-1") != pr.shared_code(a, account_id="acct-2")

    def test_a_changed_access_code_or_another_serial_is_another_code(self):
        base = pr.shared_code(_machine("a"), account_id="acct-1")
        assert base != pr.shared_code(_machine("a", access_code="new-code-after-reset"), account_id="acct-1")
        assert base != pr.shared_code(_machine("b"), account_id="acct-1")

    def test_no_secret_or_nobody_signed_in_means_no_code(self, monkeypatch):
        plain = SimpleNamespace(name="k1", host="10.0.0.9", _printer_model="voron_2")
        assert pr.shared_code(plain, account_id="acct-1") is None
        monkeypatch.setattr(auth_session, "resolve_api_bearer",
                            lambda *a, **k: auth_session.ApiBearer(token="", state="signed_out"))
        assert pr.shared_code(_machine("a")) is None

    def test_the_code_reveals_neither_the_serial_nor_the_access_code(self):
        a = _machine("a")
        code = pr.shared_code(a, account_id="acct-1")
        assert a.serial.lower() not in code.lower() and a.access_code not in code


# ---------------------------------------------------------------------------
# The courier
# ---------------------------------------------------------------------------


class TestWhatGoesUp:
    def test_a_mark_goes_up_once_with_only_the_named_fields_beside_the_printers(self, db, signed_in, registry, account):
        a = registry.get("a")
        pr.mark(a, "needed", note="belt clicking, see note on the bench at 12 Elm St", db=db)
        out = courier.sync(db=db, registry=registry)
        assert out["synced"] is True and out["sent"] == 1 and out["kept"] == 1
        (call,) = account["calls"]
        assert call["path"] == courier.SYNC_PATH
        (mark,) = call["body"]["marks"]
        assert set(mark) == {"source_id", "status", "marked_at", "created_at", "machine_code"}
        assert mark["status"] == "needed" and len(mark["source_id"]) == 32
        assert mark["machine_code"].startswith("id:") and len(mark["machine_code"]) == 27
        flat = json.dumps(call["body"])
        for leak in ("belt clicking", "Elm St", a.serial, a.access_code, "192.168", '"a"', '"name"'):
            assert leak not in flat, leak
        printers = {p["machine_code"]: p for p in call["body"]["machines"]}
        assert set(printers) == {outbound_guard.machine_code(machine_fingerprint(a)),
                                 outbound_guard.machine_code(machine_fingerprint(registry.get("b")))}
        for p in printers.values():
            assert set(p) == {"machine_code", "model", "shared_code"}
            assert p["model"] == "bambu_a1" and p["shared_code"].startswith("pc:")
        # Taken once: the next sync carries no mark, only the announcements.
        assert courier.sync(db=db, registry=registry)["sent"] == 0

    def test_a_done_carries_its_kind_and_a_clear_goes_up_too(self, db, signed_in, registry, account):
        a = registry.get("a")
        pr.mark(a, "done", kind="nozzle", db=db)
        pr.mark(a, "clear", db=db)
        courier.sync(db=db, registry=registry)
        marks = account["calls"][-1]["body"]["marks"]
        assert [(m["status"], m.get("kind")) for m in marks] == [("done", "nozzle"), ("clear", None)]

    def test_signed_out_nothing_goes_up_and_the_record_stands(self, db, registry, account, monkeypatch):
        monkeypatch.setattr(auth_session, "resolve_api_bearer",
                            lambda *a, **k: auth_session.ApiBearer(token="", state="signed_out"))
        pr.mark(registry.get("a"), "needed", db=db)
        assert courier.sync(db=db, registry=registry) == {"synced": False, "why": "signed_out"}
        assert account["calls"] == []
        assert db.printer_condition(machine_fingerprint(registry.get("a")))["state"] == "needs_repair"
        assert len(db.repairs_owed_to_account()) == 1  # still owed, for when they sign in

    def test_a_refused_sync_leaves_the_marks_owed(self, db, signed_in, registry, monkeypatch):
        import kiln.history_courier as hc

        monkeypatch.setattr(hc, "_request", lambda *a, **k: {"status": "error", "success": False, "code": "DOWN"})
        pr.mark(registry.get("a"), "needed", db=db)
        out = courier.sync(db=db, registry=registry)
        assert out["synced"] is False and out["why"] == "not_taken" and out["code"] == "DOWN"
        assert len(db.repairs_owed_to_account()) == 1

    def test_the_hosted_server_and_the_switch_send_nothing(self, monkeypatch):
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        assert courier._enabled() is False
        monkeypatch.delenv("KILN_HOSTED_MULTITENANT")
        monkeypatch.setenv(courier.SWITCH_ENV, "0")
        assert courier._enabled() is False

    def test_every_mark_offers_itself_to_the_courier(self, db, registry, monkeypatch):
        offered = []
        monkeypatch.setattr(courier, "push_soon", lambda: offered.append(True))
        pr.mark(registry.get("a"), "needed", db=db)
        pr.mark(registry.get("a"), "clear", db=db)
        assert offered == [True, True]


class TestWhatComesDown:
    def _held(self, registry, name, **mark):
        code = outbound_guard.machine_code(machine_fingerprint(registry.get(name)))
        row = {"source_id": "f" * 32, "status": "needed", "marked_at": time.time() - DAY,
               "created_at": time.time() - DAY, **mark}
        return code, row

    def test_a_mark_made_elsewhere_lands_in_this_computers_record_once(self, db, signed_in, registry, account):
        code, row = self._held(registry, "a")
        account["held"][code] = [row]
        a = registry.get("a")
        assert db.printer_condition(machine_fingerprint(a))["state"] == "ok"
        out = courier.sync(db=db, registry=registry)
        assert out["absorbed"] == 1
        cond = db.printer_condition(machine_fingerprint(a))
        assert cond["state"] == "needs_repair" and abs(cond["since"] - row["marked_at"]) < 1
        (mark,) = cond["marks"]
        assert mark["origin"] == "account" and mark["account_source_id"] == row["source_id"]
        assert mark["printer_name"] == "a" and mark["note"] is None
        # Brought down again: nothing doubles.
        assert courier.sync(db=db, registry=registry)["absorbed"] == 0
        assert len(db.printer_condition(machine_fingerprint(a))["marks"]) == 1
        # The absorbed mark is the account's; it is never owed back up.
        assert db.repairs_owed_to_account() == []

    def test_this_computers_own_mark_coming_back_is_itself(self, db, signed_in, registry, account):
        a = registry.get("a")
        pr.mark(a, "done", kind="belts", db=db)
        courier.sync(db=db, registry=registry)
        sent = account["calls"][-1]["body"]["marks"][0]
        code = outbound_guard.machine_code(machine_fingerprint(a))
        account["held"][code] = [sent]
        assert courier.sync(db=db, registry=registry)["absorbed"] == 0
        assert len(db.printer_condition(machine_fingerprint(a))["marks"]) == 1

    def test_the_two_records_merge_by_the_moment_of_each_mark(self, db, signed_in, registry, account):
        a = registry.get("a")
        fp = machine_fingerprint(a)
        pr.mark(a, "needed", db=db)                      # here, now
        code, row = self._held(registry, "a", source_id="e" * 32, status="done", kind="nozzle",
                               marked_at=time.time() + 30, created_at=time.time() + 30)  # elsewhere, after
        account["held"][code] = [row]
        courier.sync(db=db, registry=registry)
        cond = db.printer_condition(fp)
        assert cond["state"] == "repaired" and cond["kind"] == "nozzle"
        assert [m["status"] for m in cond["marks"]] == ["needed", "done"]

    def test_a_mark_the_account_cannot_be_trusted_with_is_ignored(self, db, signed_in, registry, account):
        code, _ = self._held(registry, "a")
        account["held"][code] = [
            {"source_id": "not-an-id", "status": "needed", "marked_at": time.time()},
            {"source_id": "d" * 32, "status": "exploded", "marked_at": time.time()},
            {"source_id": "c" * 32, "status": "needed", "marked_at": "soon"},
        ]
        assert courier.sync(db=db, registry=registry)["absorbed"] == 0

    def test_the_questions_are_kept_for_the_printer_they_are_about(self, db, signed_in, registry, account):
        a, b = registry.get("a"), registry.get("b")
        code_a = outbound_guard.machine_code(machine_fingerprint(a))
        account["asks"] = [
            {"id": "q:" + "1" * 16, "this_code": code_a, "words": "A Bambu Lab A1 was marked needs repair from "
             "another computer on Oct 20. Is it this one (a)?", "other_label": "Bambu Lab A1 · 2",
             "other_model": "bambu_a1", "other_state": "needs_repair", "other_since": "2026-10-20T10:00:00+00:00"},
            {"id": "q:" + "2" * 16, "this_code": "id:" + "0" * 24, "words": "not ours"},
            {"id": "bad", "this_code": code_a, "words": "no id"},
        ]
        out = courier.sync(db=db, registry=registry)
        (q,) = out["questions"]
        assert q["id"] == "q:" + "1" * 16 and q["printer"] == "a" and "Is it this one (a)?" in q["words"]
        assert q["other"]["other_label"] == "Bambu Lab A1 · 2"
        assert courier.open_questions(a) == [q] and courier.open_questions(b) == []
        assert courier.open_questions(machine_fingerprint(a)) == [q]


class TestTheAnswer:
    def test_a_yes_reaches_the_account_and_the_marks_come_down(self, db, signed_in, registry, account):
        a = registry.get("a")
        code = outbound_guard.machine_code(machine_fingerprint(a))
        account["held"][code] = [{"source_id": "b" * 32, "status": "needed", "marked_at": time.time() - DAY,
                                 "created_at": time.time() - DAY}]
        out = pr.answer_identity(a, "q:" + "a" * 16, same=True, db=db)
        assert out["recorded"] is True and out["answer"] == "same" and out["linked"] is True
        assert [c["path"] for c in account["calls"]] == [courier.ANSWER_PATH, courier.SYNC_PATH]
        assert account["calls"][0]["body"] == {"question_id": "q:" + "a" * 16, "answer": "same"}
        assert out["condition"]["state"] == "needs_repair"
        assert "one record now" in out["next"]

    def test_a_no_is_sent_as_different(self, db, signed_in, registry, account):
        out = pr.answer_identity("a", "q:" + "a" * 16, same=False, db=db)
        assert account["calls"][0]["body"]["answer"] == "different" and "keeps its own record" in out["next"]

    def test_a_question_id_off_the_list_and_a_signed_out_computer_are_refused(self, db, registry, account, monkeypatch):
        with pytest.raises(pr.RepairRefused) as refused:
            pr.answer_identity("a", "nonsense", same=True, db=db)
        assert refused.value.code == "QUESTION_UNKNOWN"
        monkeypatch.setattr(auth_session, "resolve_api_bearer",
                            lambda *a, **k: auth_session.ApiBearer(token="", state="signed_out"))
        with pytest.raises(pr.RepairRefused) as refused:
            pr.answer_identity("a", "q:" + "a" * 16, same=True, db=db)
        assert refused.value.code == "KILN_ACCOUNT_NOT_PAIRED"
        assert account["calls"] == []

    def test_the_hosted_server_cannot_answer_for_a_computer(self, db, registry, monkeypatch):
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        with pytest.raises(pr.RepairRefused) as refused:
            pr.answer_identity("a", "q:" + "a" * 16, same=True, db=db)
        assert refused.value.code == pr.CODE_LOCAL_ONLY


# ---------------------------------------------------------------------------
# The clear rule across two records
# ---------------------------------------------------------------------------


class TestTheClearRule:
    def test_a_clear_takes_back_what_was_written_before_it_not_a_later_backdated_repair(self, db, registry):
        a = registry.get("a")
        fp = machine_fingerprint(a)
        pr.mark(a, "needed", when=time.time() - 10 * DAY, db=db)
        pr.mark(a, "clear", db=db)
        assert db.printer_condition(fp)["state"] == "ok"
        pr.mark(a, "done", kind="bed", when=time.time() - 5 * DAY, db=db)   # dated between the two
        cond = db.printer_condition(fp)
        assert cond["state"] == "repaired" and cond["kind"] == "bed"
        assert [m["status"] for m in cond["marks"]] == ["needed", "done", "clear"]  # by moment

    def test_the_standing_rule_is_one_function_both_records_read(self):
        marks = [
            {"status": "needed", "marked_at": 1.0, "created_at": 1.0},
            {"status": "done", "marked_at": 2.0, "created_at": 2.0},
            {"status": "clear", "marked_at": 3.0, "created_at": 3.0},
        ]
        assert [m["status"] for m in KilnDB.standing_repair_marks(marks)] == ["needed"]


# ---------------------------------------------------------------------------
# The doors
# ---------------------------------------------------------------------------


class _FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


@pytest.fixture
def tool(monkeypatch, db, registry):
    import kiln.server as srv
    from kiln import persistence as persistence_mod
    from kiln.plugins import printer_management_tools

    monkeypatch.setattr(persistence_mod, "get_db", lambda: db)
    monkeypatch.setattr(srv, "_check_auth", lambda scope: None)
    monkeypatch.setattr(srv, "ensure_runtime_config", lambda: None)
    mcp = _FakeMCP()
    printer_management_tools.plugin.register(mcp)
    return mcp.tools["set_printer_repair"]


class TestTheToolDoor:
    def test_a_mark_signed_in_says_it_reached_the_account_and_carries_the_questions(self, tool, signed_in, account, registry):
        code_a = outbound_guard.machine_code(machine_fingerprint(registry.get("a")))
        account["asks"] = [{"id": "q:" + "1" * 16, "this_code": code_a, "words": "Is it this one (a)?"}]
        out = tool("a", "needed")
        assert out["success"] is True and out["status"] == "needed"
        assert out["account"]["synced"] is True and out["account"]["sent"] == 1
        assert out["questions"][0]["id"] == "q:" + "1" * 16 and "questions" not in out["account"]

    def test_a_mark_signed_out_carries_no_account_block(self, tool, account, monkeypatch):
        monkeypatch.setattr(auth_session, "resolve_api_bearer",
                            lambda *a, **k: auth_session.ApiBearer(token="", state="signed_out"))
        out = tool("a", "needed")
        assert out["success"] is True and "account" not in out and account["calls"] == []

    def test_same_as_and_different_from_answer_the_account(self, tool, signed_in, account):
        out = tool("a", same_as="q:" + "1" * 16)
        assert out["success"] is True and out["answer"] == "same" and out["printer"] == "a"
        assert account["calls"][0]["body"] == {"question_id": "q:" + "1" * 16, "answer": "same"}
        out = tool("a", different_from="q:" + "2" * 16)
        assert out["answer"] == "different"

    def test_an_answer_with_a_status_or_both_answers_is_refused_and_nothing_is_written(self, tool, signed_in, account, db, registry):
        assert tool("a", "needed", same_as="q:" + "1" * 16)["error"]["code"] == "REPAIR_ANSWER_AMBIGUOUS"
        assert tool("a", same_as="q:" + "1" * 16, different_from="q:" + "1" * 16)["error"]["code"] == "REPAIR_ANSWER_AMBIGUOUS"
        assert tool("a")["error"]["code"] == pr.CODE_BAD_STATUS
        assert account["calls"] == []
        assert db.printer_condition(machine_fingerprint(registry.get("a")))["marks"] == []


class TestTheCommandDoor:
    def test_the_command_answers_a_question_through_the_same_engine(self, monkeypatch, signed_in, account, registry):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        monkeypatch.setattr("kiln.server.ensure_runtime_config", lambda: None)
        result = CliRunner().invoke(cli, ["machine", "repair", "a", "--same-as", "q:" + "1" * 16, "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["answer"] == "same" and data["data"]["printer"] == "a"
        assert account["calls"][0]["path"] == courier.ANSWER_PATH

    def test_the_command_refuses_a_status_with_an_answer_and_no_status_at_all(self, monkeypatch, signed_in, account):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        monkeypatch.setattr("kiln.server.ensure_runtime_config", lambda: None)
        out = CliRunner().invoke(cli, ["machine", "repair", "a", "needed", "--same-as", "q:" + "1" * 16, "--json"])
        assert out.exit_code == 1 and "REPAIR_ANSWER_AMBIGUOUS" in out.output
        out = CliRunner().invoke(cli, ["machine", "repair", "a", "--json"])
        assert out.exit_code == 1 and "REPAIR_STATUS_UNKNOWN" in out.output
        assert account["calls"] == []


class TestTheHostDialogDoor:
    """The question put in the host's own dialog, before the mark."""

    def _ask(self, monkeypatch, registry, account, *, picked: str, arguments: dict | None = None):
        import asyncio

        import kiln.server as srv
        from kiln import mcp_compat

        monkeypatch.setattr(srv, "_hosted_now", lambda: False)
        monkeypatch.setattr(srv, "host_can_ask_the_user", lambda mcp, ctx: True)
        monkeypatch.setattr(srv.screen_code, "host_dialog_hook", lambda name: None)
        monkeypatch.setattr(srv, "_host_client_name", lambda ctx: "claude")
        monkeypatch.setattr(srv, "_audit", lambda *a, **k: None)
        asked: list[dict] = []

        async def fake_ask(ctx, message, *, title, yes, no):
            asked.append({"message": message, "title": title, "yes": yes, "no": no})
            return picked

        monkeypatch.setattr(mcp_compat, "ask_user_yes_or_no", fake_ask)
        code_a = outbound_guard.machine_code(machine_fingerprint(registry.get("a")))
        account["asks"] = [{"id": "q:" + "7" * 16, "this_code": code_a, "words": "Is it this one (a)?"}]
        asyncio.run(srv._ask_the_person_about_the_machine(
            "set_printer_repair", arguments if arguments is not None else {"printer_name": "a", "status": "needed"},
            ctx=object(),
        ))
        return asked

    def test_a_yes_in_the_dialog_links_the_records(self, monkeypatch, signed_in, account, registry, db):
        monkeypatch.setattr("kiln.persistence.get_db", lambda: db)
        asked = self._ask(monkeypatch, registry, account, picked="yes")
        assert asked == [{"message": "Is it this one (a)?", "title": "IsThisThePrinter",
                          "yes": "Yes, the same printer", "no": "No, a different printer"}]
        answers = [c["body"] for c in account["calls"] if c["path"] == courier.ANSWER_PATH]
        assert answers == [{"question_id": "q:" + "7" * 16, "answer": "same"}]

    def test_a_no_is_remembered_and_an_unavailable_answer_answers_nothing(self, monkeypatch, signed_in, account, registry, db):
        monkeypatch.setattr("kiln.persistence.get_db", lambda: db)
        self._ask(monkeypatch, registry, account, picked="no")
        answers = [c["body"] for c in account["calls"] if c["path"] == courier.ANSWER_PATH]
        assert answers == [{"question_id": "q:" + "7" * 16, "answer": "different"}]
        account["calls"].clear()
        self._ask(monkeypatch, registry, account, picked="unavailable")
        assert [c["path"] for c in account["calls"]] == [courier.SYNC_PATH]

    def test_a_call_that_is_itself_an_answer_is_never_asked(self, monkeypatch, signed_in, account, registry, db):
        monkeypatch.setattr("kiln.persistence.get_db", lambda: db)
        asked = self._ask(monkeypatch, registry, account, picked="yes",
                          arguments={"printer_name": "a", "same_as": "q:" + "7" * 16})
        assert asked == [] and account["calls"] == []

    def test_signed_out_nobody_is_asked(self, monkeypatch, account, registry, db):
        monkeypatch.setattr(auth_session, "resolve_api_bearer",
                            lambda *a, **k: auth_session.ApiBearer(token="", state="signed_out"))
        monkeypatch.setattr("kiln.persistence.get_db", lambda: db)
        assert self._ask(monkeypatch, registry, account, picked="yes") == [] and account["calls"] == []
