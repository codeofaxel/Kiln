"""A note in the person's own words on how a print came out, on the print row.

The row already carried ``notes``; what it did not say was WHO wrote them, so a
system string ("auto-recorded on terminal state transition ...") and a person's
"the back-left corner lifted at layer 40" were the same kind of thing.  Now a
note is labelled ``note_by``: ``person`` or ``agent``.  Pinned, each against
the real ``KilnDB``:

* the row keeps the note and who said it; the note is cut to its cap and
  cleaned of control characters; a label outside the two words is refused;
* the merge rule on this computer: a person's note is never overwritten by an
  agent's; an agent's note is replaced by a later agent's only when the
  ending word is at least as strong; a person's note is replaced only by a
  person's; a note nobody labelled (a system string) never replaces a
  labelled one;
* the courier carries a labelled note up with its label, never an unlabelled
  one, and a note added later makes the row owed again;
* the one record door: a person's words default to ``person``, an agent may
  say ``agent``, a hook's own string stays unlabelled.
"""

from __future__ import annotations

import pytest

from kiln import auth_session, outcome_courier
from kiln.outcome_note import NOTE_MAX_CHARS, clean_note
from kiln.persistence import KilnDB


@pytest.fixture
def db(tmp_path):
    instance = KilnDB(db_path=str(tmp_path / "kiln.db"))
    yield instance
    instance.close()


def _start(db, job="start:a1:1"):
    db.open_pending_outcome(
        job_id=job, printer_name="Shop A1", file_name="bracket.3mf", material_type="PLA",
        identity={"version_id": "v-1", "design_id": "d-1", "branch_id": "b-1",
                  "machine_fingerprint": "serial:01P00A123456789", "printer_model": "bambu_a1",
                  "nozzle_diameter_mm": 0.4},
    )


def _end(db, outcome, by, note=None, note_by=None, job="start:a1:1"):
    row = {"job_id": job, "printer_name": "Shop A1", "outcome": outcome, "determined_by": by}
    if note is not None:
        row["notes"] = note
    if note_by is not None:
        row["note_by"] = note_by
    db.save_print_outcome(row)
    return db.get_print_outcome(job)


# --- the words themselves -------------------------------------------------

def test_a_note_is_cleaned_and_capped():
    assert clean_note("  back-left corner\x00 lifted\x07 at layer 40  ") == "back-left corner lifted at layer 40"
    assert clean_note("line one\nline two") == "line one\nline two", "a line break is kept"
    assert clean_note("") is None and clean_note("   ") is None and clean_note(None) is None
    assert len(clean_note("x" * (NOTE_MAX_CHARS + 500))) == NOTE_MAX_CHARS


def test_the_row_keeps_the_note_and_who_said_it(db):
    _start(db)
    row = _end(db, "failed", "user_reported", note="the back-left corner lifted\x00 at layer 40", note_by="person")
    assert row["notes"] == "the back-left corner lifted at layer 40"
    assert row["note_by"] == "person"


def test_a_label_outside_the_two_words_is_refused(db):
    _start(db)
    with pytest.raises(ValueError):
        _end(db, "failed", "user_reported", note="hm", note_by="robot")


# --- the merge rule on this computer ---------------------------------------

def test_a_persons_note_is_never_overwritten_by_an_agents(db):
    _start(db)
    _end(db, "failed", "user_reported", note="corner lifted", note_by="person")
    row = _end(db, "failed", "user_reported", note="printer reported a fault", note_by="agent")
    assert (row["notes"], row["note_by"]) == ("corner lifted", "person")


def test_a_persons_note_is_replaced_only_by_a_persons(db):
    _start(db)
    _end(db, "failed", "user_reported", note="corner lifted", note_by="person")
    row = _end(db, "failed", "user_reported", note="actually the whole back edge", note_by="person")
    assert (row["notes"], row["note_by"]) == ("actually the whole back edge", "person")


def test_an_agents_note_yields_to_a_later_agents_only_when_the_word_is_as_strong(db):
    _start(db)
    _end(db, "failed", "observed", note="spaghetti at layer 12", note_by="agent")
    # A weaker word later: the ending is held, and so is the note.
    row = _end(db, "success", "inferred", note="printer idle, assumed fine", note_by="agent")
    assert row["outcome"] == "failed"
    assert (row["notes"], row["note_by"]) == ("spaghetti at layer 12", "agent")
    # An equal word later: the note moves with it.
    row = _end(db, "failed", "observed", note="spaghetti at layer 12, part detached", note_by="agent")
    assert (row["notes"], row["note_by"]) == ("spaghetti at layer 12, part detached", "agent")


def test_a_system_string_never_replaces_a_labelled_note(db):
    _start(db)
    _end(db, "failed", "user_reported", note="corner lifted", note_by="person")
    row = _end(db, "failed", "user_reported", note="auto-recorded by the hook")
    assert (row["notes"], row["note_by"]) == ("corner lifted", "person")


# --- the courier -----------------------------------------------------------

@pytest.fixture
def signed_in(monkeypatch):
    monkeypatch.setattr(
        auth_session, "resolve_api_bearer",
        lambda *a, **k: auth_session.ApiBearer(token="tok", state="license"),
    )


@pytest.fixture
def sent(monkeypatch):
    calls: list[dict] = []

    def fake_request(tool, method, path, body=None, **kw):
        calls.append({"path": path, "body": body})
        rows = (body or {}).get("rows") or []
        return {"status": "success", "success": True,
                "kept": [r["source_job_id"] for r in rows], "held": [], "refused": 0}

    import kiln.history_courier as hc

    monkeypatch.setattr(hc, "_request", fake_request)
    monkeypatch.setattr(outcome_courier, "_kept_here", lambda rows: set())
    return calls


def test_a_labelled_note_goes_up_with_its_label(db, signed_in, sent):
    _start(db)
    _end(db, "failed", "user_reported", note="corner lifted", note_by="person")
    assert outcome_courier.push_owed(db=db)["sent"] == 1
    (row,) = sent[-1]["body"]["rows"]
    assert (row["note"], row["note_by"]) == ("corner lifted", "person")


def test_an_unlabelled_note_stays_on_this_computer(db, signed_in, sent):
    _start(db)
    _end(db, "success", "observed", note="auto-recorded on terminal state transition")
    assert outcome_courier.push_owed(db=db)["sent"] == 1
    (row,) = sent[-1]["body"]["rows"]
    assert "note" not in row and "note_by" not in row
    assert "terminal state" not in repr(row)


def test_a_note_added_later_is_owed_again(db, signed_in, sent):
    _start(db)
    _end(db, "failed", "user_reported")
    outcome_courier.push_owed(db=db)
    assert outcome_courier.push_owed(db=db)["sent"] == 0
    _end(db, "failed", "user_reported", note="corner lifted", note_by="person")
    assert outcome_courier.push_owed(db=db)["sent"] == 1
    assert sent[-1]["body"]["rows"][0]["note"] == "corner lifted"
    assert outcome_courier.push_owed(db=db)["sent"] == 0, "the same note is not owed twice"


# --- the one record door ---------------------------------------------------

@pytest.fixture
def tmp_kiln_env(tmp_path, monkeypatch):
    import os

    monkeypatch.setenv("KILN_DB_PATH", str(tmp_path / "kiln.db"))
    monkeypatch.setenv("HOME", str(tmp_path))
    if os.name == "nt":
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
    import kiln.persistence as _p

    monkeypatch.setattr(_p, "_db", None, raising=False)
    pro_hook_was_installed = False
    try:
        from kiln_pro.print_learning import auto_record as _pro_auto_record

        pro_hook_was_installed = _pro_auto_record.uninstall_auto_record_hook()
    except ImportError:
        pass
    yield tmp_path
    if pro_hook_was_installed:
        _pro_auto_record.install_auto_record_hook()
    monkeypatch.setattr(_p, "_db", None, raising=False)


@pytest.fixture
def door(tmp_kiln_env, monkeypatch):
    import kiln.server as _srv
    from kiln.persistence import get_db
    from kiln.plugins import learning_tools as lt

    monkeypatch.setattr(_srv, "_check_auth", lambda *_a, **_k: None)
    return lt.record_print_outcome, get_db()


def test_a_persons_words_default_to_person(door):
    record, db = door
    out = record(job_id="j1", outcome="failed", printer_name="p", notes="corner lifted\x00")
    assert out.get("success") is True, out
    row = db.get_print_outcome("j1")
    assert (row["notes"], row["note_by"]) == ("corner lifted", "person")
    assert out["note_by"] == "person"


def test_an_agent_may_say_what_it_saw(door):
    record, db = door
    out = record(job_id="j2", outcome="failed", printer_name="p", determined_by="observed",
                 notes="layer 40 lifted at the back-left corner", note_by="agent")
    assert out.get("success") is True, out
    assert db.get_print_outcome("j2")["note_by"] == "agent"


def test_a_watched_ending_with_words_is_the_agents_by_default(door):
    record, db = door
    out = record(job_id="j3", outcome="success", printer_name="p", determined_by="observed",
                 notes="finished clean")
    assert out.get("success") is True, out
    assert db.get_print_outcome("j3")["note_by"] == "agent"


def test_a_hooks_own_string_stays_unlabelled(door):
    record, db = door
    out = record(job_id="j4", outcome="success", printer_name="p", auto_recorded=True,
                 notes="auto-recorded on terminal state transition")
    assert out.get("success") is True, out
    assert db.get_print_outcome("j4")["note_by"] is None


def test_a_bad_label_is_refused_at_the_door(door):
    record, _db = door
    out = record(job_id="j5", outcome="failed", printer_name="p", notes="hm", note_by="robot")
    assert out.get("success") is False and out["error"]["code"] == "VALIDATION_ERROR"
