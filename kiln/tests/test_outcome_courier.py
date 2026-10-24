"""The courier that copies a settled print row to the person's Kiln account.

What is pinned, each against the real ``KilnDB``:

* a settled row of a kept version goes up once, with only the named fields --
  no printer name, file name, serial, notes or settings;
* the machine goes as a one-way code, never the fingerprint itself;
* the row's account id is minted once and survives the job id being swapped
  for the printer's own label (often the file's name);
* a later, surer ending makes the row owed again; the same ending does not;
* a foreign file, a pending row, a locked design and a signed-out computer
  send nothing;
* every settled write through ``save_print_outcome`` offers itself, and a
  pending one does not;
* the start carries an account id the connector handed it, and refuses one
  that is not shaped like one.
"""

from __future__ import annotations

import pytest

from kiln import auth_session, outcome_courier
from kiln.persistence import KilnDB


@pytest.fixture
def db(tmp_path):
    instance = KilnDB(db_path=str(tmp_path / "kiln.db"))
    yield instance
    instance.close()


@pytest.fixture
def signed_in(monkeypatch):
    monkeypatch.setattr(
        auth_session, "resolve_api_bearer",
        lambda *a, **k: auth_session.ApiBearer(token="tok", state="license"),
    )


@pytest.fixture
def sent(monkeypatch):
    """Every push the courier makes, answered as the server would."""
    calls: list[dict] = []

    def fake_request(tool, method, path, body=None, **kw):
        calls.append({"tool": tool, "method": method, "path": path, "body": body})
        rows = (body or {}).get("rows") or []
        return {"status": "success", "success": True,
                "kept": [r["source_job_id"] for r in rows], "held": [], "refused": 0}

    import kiln.history_courier as hc

    monkeypatch.setattr(hc, "_request", fake_request)
    monkeypatch.setattr(outcome_courier, "_kept_here", lambda rows: set())
    return calls


def _print(db, job="start:a1:1", outcome="success", **extra):
    db.open_pending_outcome(
        job_id=job, printer_name="Shop A1", file_name="my_secret_bracket.3mf",
        material_type="PLA",
        identity={"version_id": "v-1", "design_id": "d-1", "branch_id": "b-1",
                  "machine_fingerprint": "serial:01P00A123456789", "printer_model": "bambu_a1",
                  "nozzle_diameter_mm": 0.4, **extra.pop("identity", {})},
    )
    if outcome != "pending":
        db.save_print_outcome({
            "job_id": job, "printer_name": "Shop A1", "outcome": outcome,
            "determined_by": extra.pop("determined_by", "observed"),
            "notes": "left it overnight", "settings": {"temp_tool": 210}, **extra,
        })


def test_a_settled_row_goes_up_once_with_only_the_named_fields(db, signed_in, sent):
    _print(db)
    out = outcome_courier.push_owed(db=db)
    assert out["sent"] == 1
    (call,) = sent
    assert call["path"] == "/api/cloud/outcomes/push"
    (row,) = call["body"]["rows"]
    assert set(row) <= {
        "source_job_id", "version_id", "design_id", "branch_id", "machine_code",
        "printer_model", "material", "nozzle_diameter_mm", "ending", "determined_by",
        "failure_mode", "started_at", "ended_at",
    }
    assert (row["version_id"], row["ending"], row["determined_by"]) == ("v-1", "success", "observed")
    flat = repr(row)
    for leak in ("Shop A1", "my_secret_bracket", "01P00A123456789", "overnight", "temp_tool"):
        assert leak not in flat
    assert row["machine_code"].startswith("id:") and len(row["machine_code"]) == 27
    assert outcome_courier.push_owed(db=db) == {"sent": 0, "why": "nothing_owed"}


def test_the_account_id_survives_the_job_id_being_relabelled(db, signed_in, sent):
    db.open_pending_outcome(
        job_id="start:a1:2", printer_name="Shop A1", file_name="bracket.3mf",
        identity={"version_id": "v-1"},
    )
    # The printer reports its own label for the job; the pending row adopts it.
    db.save_print_outcome({"job_id": "bracket", "printer_name": "Shop A1",
                           "file_name": "bracket.3mf", "outcome": "unknown"})
    outcome_courier.push_owed(db=db)
    first = sent[-1]["body"]["rows"][0]["source_job_id"]
    db.save_print_outcome({"job_id": "bracket", "printer_name": "Shop A1",
                           "outcome": "failed", "determined_by": "user_reported"})
    outcome_courier.push_owed(db=db)
    second = sent[-1]["body"]["rows"][0]
    assert second["source_job_id"] == first
    assert "bracket" not in first and len(first) == 32
    assert second["ending"] == "failed"


def test_a_surer_ending_is_owed_again_and_the_same_one_is_not(db, signed_in, sent):
    _print(db, outcome="unknown", determined_by=None)
    outcome_courier.push_owed(db=db)
    assert outcome_courier.push_owed(db=db)["sent"] == 0
    db.save_print_outcome({"job_id": "start:a1:1", "printer_name": "Shop A1",
                           "outcome": "success", "determined_by": "user_reported"})
    out = outcome_courier.push_owed(db=db)
    assert out["sent"] == 1
    assert sent[-1]["body"]["rows"][0]["determined_by"] == "user_reported"


def test_what_is_never_sent(db, signed_in, sent, monkeypatch):
    # A file that is no kept design, and a print still running.
    db.save_print_outcome({"job_id": "foreign", "printer_name": "Shop A1", "outcome": "success",
                           "determined_by": "observed"})
    _print(db, job="start:a1:9", outcome="pending")
    assert outcome_courier.push_owed(db=db) == {"sent": 0, "why": "nothing_owed"}
    # A design locked to this computer: kept here, and still owed, so it goes
    # up if the person unlocks it.
    monkeypatch.setattr(outcome_courier, "_kept_here", lambda rows: {r["id"] for r in rows})
    _print(db, job="start:a1:3")
    out = outcome_courier.push_owed(db=db)
    assert out["why"] == "locked_to_this_computer" and sent == []
    assert len(db.outcomes_owed_to_account()) == 1


def test_a_signed_out_computer_sends_nothing(db, sent, monkeypatch):
    monkeypatch.setattr(
        auth_session, "resolve_api_bearer",
        lambda *a, **k: auth_session.ApiBearer(token="", state="signed_out"),
    )
    _print(db)
    assert outcome_courier.push_owed(db=db) == {"sent": 0, "why": "signed_out"}
    assert sent == []


def test_a_refused_push_stays_owed(db, signed_in, monkeypatch):
    import kiln.history_courier as hc

    monkeypatch.setattr(hc, "_request", lambda *a, **k: {"status": "error", "success": False,
                                                          "code": "STORE_UNAVAILABLE"})
    monkeypatch.setattr(outcome_courier, "_kept_here", lambda rows: set())
    _print(db)
    assert outcome_courier.push_owed(db=db)["why"] == "not_taken"
    assert len(db.outcomes_owed_to_account()) == 1


def test_every_settled_write_offers_itself(db, monkeypatch):
    offers: list[int] = []
    monkeypatch.setattr(outcome_courier, "push_soon", lambda: offers.append(1))
    _print(db, outcome="pending")
    assert offers == []
    db.save_print_outcome({"job_id": "start:a1:1", "printer_name": "Shop A1",
                           "outcome": "failed", "determined_by": "inferred"})
    assert offers == [1]


def test_push_soon_is_quiet_under_tests_unless_asked(monkeypatch):
    monkeypatch.delenv(outcome_courier.TEST_OPT_IN_ENV, raising=False)
    assert outcome_courier._enabled() is False
    monkeypatch.setenv(outcome_courier.TEST_OPT_IN_ENV, "1")
    assert outcome_courier._enabled() is True
    monkeypatch.setenv(outcome_courier.SWITCH_ENV, "0")
    assert outcome_courier._enabled() is False


def test_the_start_carries_an_account_id_it_was_handed(db, signed_in, sent):
    from kiln.auto_record_hook import print_identity_at_start

    class _Adapter:
        name = "bambu"

        def declared_printer_model(self):
            return None

    good = "a" * 32
    assert print_identity_at_start(_Adapter(), {"account_sync_id": good}).get("account_sync_id") == good
    assert "account_sync_id" not in print_identity_at_start(_Adapter(), {"account_sync_id": "bracket.3mf"})
    _print(db, identity={"account_sync_id": good})
    outcome_courier.push_owed(db=db)
    assert sent[-1]["body"]["rows"][0]["source_job_id"] == good


class _Vault:
    """This computer's history records, as the courier reads them."""

    locked: set = set()
    versions: dict = {}
    branches: dict = {}
    broken = False

    def __init__(self):
        if self.broken:
            import sqlite3

            raise sqlite3.DatabaseError("file is not a database")

    def design_of_version(self, version_id):
        return self.versions.get(version_id)

    def design_of_branch(self, branch_id):
        return self.branches.get(branch_id)

    def is_locked(self, design_id):
        return design_id in self.locked

    def close(self):
        return None


@pytest.fixture
def vault(tmp_path, monkeypatch):
    import kiln.history_vault as hv

    (tmp_path / "history_vault.db").write_bytes(b"x")
    monkeypatch.setattr(hv, "_kiln_home", lambda: tmp_path)
    monkeypatch.setattr(hv, "HistoryVault", _Vault)
    _Vault.locked, _Vault.versions, _Vault.branches, _Vault.broken = set(), {}, {}, False
    return _Vault


def test_a_row_naming_only_its_version_is_judged_by_its_design(db, vault):
    """A print row may carry a version id and no design id; the design the
    records say that version belongs to decides, and a locked one stays here."""
    vault.versions = {"v-only": "secret-design"}
    vault.locked = {"secret-design"}
    db.open_pending_outcome(job_id="j", printer_name="Shop A1", identity={"version_id": "v-only"})
    db.save_print_outcome({"job_id": "j", "printer_name": "Shop A1", "outcome": "success",
                           "determined_by": "observed"})
    rows = db.outcomes_owed_to_account()
    assert outcome_courier._kept_here(rows) == {rows[0]["id"]}
    vault.locked = set()
    assert outcome_courier._kept_here(rows) == set()


def test_records_that_cannot_be_read_hold_everything_back(db, vault):
    vault.broken = True
    _print(db)
    rows = db.outcomes_owed_to_account()
    assert outcome_courier._kept_here(rows) == {r["id"] for r in rows}


def test_no_records_at_all_holds_nothing_back(db, tmp_path, monkeypatch):
    import kiln.history_vault as hv

    monkeypatch.setattr(hv, "_kiln_home", lambda: tmp_path / "nowhere")
    _print(db)
    assert outcome_courier._kept_here(db.outcomes_owed_to_account()) == set()
