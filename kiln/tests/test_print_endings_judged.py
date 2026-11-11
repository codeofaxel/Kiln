"""Only a bad print counts against a print; a printer's own stop counts
only against the printer.

A failed or partial ending counts against a design, a version, a material or
settings only when it is a bad print: the part came out wrong.  A machine
stop (a fault the printer named, or a failure only the printer reported)
counts against that printer and nothing else.  A power cut or a run-out spool
counts against nothing and stays in the person's history.  The rule is
:mod:`kiln.failure_vocabulary`; these tests feed every reader the same
endings.
"""

from __future__ import annotations

import itertools
import sqlite3
from collections import Counter
from unittest import mock

import pytest

from kiln import failure_vocabulary as fv
from kiln.persistence import KilnDB

MACHINE = "id:" + "a" * 24


@pytest.fixture()
def db(tmp_path):
    instance = KilnDB(db_path=str(tmp_path / "kiln.db"))
    yield instance
    instance.close()


def _end(db: KilnDB, job: str, outcome: str, mode: str | None = None, by: str | None = "observed",
         **extra) -> None:
    db.save_print_outcome({
        "job_id": job, "printer_name": "p1", "outcome": outcome, "failure_mode": mode,
        "determined_by": by, "agent_id": "mcp", "material_type": "PLA", **extra,
    })


# ─────────────────────────── the rule ────────────────────────────────────────

def test_every_cause_word_is_classified_once():
    """A new cause word fails here until it is called a defect, an outside
    cause or neither."""
    groups = (fv.PRINT_DEFECTS, fv.MACHINE_FAULTS, fv.OUTSIDE_CAUSES, frozenset({fv.UNNAMED_CAUSE}))
    assert frozenset().union(*groups) == fv.VALID_FAILURE_MODES
    assert sum(len(g) for g in groups) == len(fv.VALID_FAILURE_MODES)  # no word in two groups


@pytest.mark.parametrize(
    "outcome, mode, by, verdict",
    [
        ("failed", "power_loss", "user_reported", "outside_stop"),
        ("failed", "filament_runout", "observed", "outside_stop"),
        ("failed", "thermal_runaway", "inferred", "machine_stop"),
        ("partial", "mechanical", "seen", "machine_stop"),
        ("failed", "adhesion", "inferred", "bad_print"),
        ("failed", "clog", "observed", "bad_print"),
        ("failed", "adhesion_loss", "observed", "bad_print"),   # a classifier's spelling
        ("failed", None, "observed", "machine_stop"),
        ("failed", "other", "inferred", "machine_stop"),
        ("failed", "", None, "machine_stop"),
        ("failed", "other", "user_reported", "bad_print"),
        ("partial", None, "seen", "bad_print"),
        ("success", None, None, "success"),
        ("completed", None, "inferred", "success"),
        ("cancelled", None, "user_reported", None),
        ("unknown", None, None, None),
        ("pending", None, None, None),
    ],
)
def test_what_each_ending_counts_as(outcome, mode, by, verdict):
    assert fv.ending_verdict(outcome, mode, by) == verdict
    assert fv.is_judged_ending(outcome, mode, by) is (verdict in ("success", "bad_print"))
    assert fv.is_judged_ending(outcome, mode, by, about=fv.ABOUT_THE_MACHINE) is (
        verdict in ("success", "bad_print", "machine_stop"))


def test_the_sql_and_the_python_rule_agree_on_every_row():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE print_outcomes (outcome TEXT, failure_mode TEXT, determined_by TEXT)")
    combos = list(itertools.product(
        ("success", "failed", "partial", "cancelled", "pending", "unknown"),
        (*sorted(fv.VALID_FAILURE_MODES), None, ""),
        ("observed", "inferred", "seen", "user_reported", None),
    ))
    conn.executemany("INSERT INTO print_outcomes VALUES (?, ?, ?)", combos)
    for about in (fv.ABOUT_THE_PRINT, fv.ABOUT_THE_MACHINE):
        want = Counter(c for c in combos if fv.is_judged_ending(*c, about=about))
        for alias in ("", "p"):
            got = Counter(conn.execute(
                f"SELECT outcome, failure_mode, determined_by FROM print_outcomes {alias} "
                f"WHERE {fv.judged_ending_sql(alias, about=about)}").fetchall())
            assert got == want, (about, alias)
    with pytest.raises(ValueError):
        fv.judged_ending_sql("p; DROP TABLE print_outcomes")
    with pytest.raises(ValueError):
        fv.judged_ending_sql(about="the weather")


# ─────────────────────────── the readers ─────────────────────────────────────

def _six_endings(db: KilnDB, **extra) -> None:
    _end(db, "a", "success", **extra)
    _end(db, "b", "success", **extra)
    _end(db, "c", "failed", "adhesion", **extra)
    _end(db, "d", "failed", "power_loss", "user_reported", **extra)
    _end(db, "e", "failed", "filament_runout", "inferred", **extra)
    _end(db, "f", "failed", None, "observed", **extra)  # the printer said failed and named nothing
    _end(db, "g", "failed", "thermal_runaway", "observed", **extra)


def test_printer_insights_count_its_own_stops_and_keep_power_and_spool_apart(db):
    _six_endings(db)
    insights = db.get_printer_learning_insights("p1")
    assert insights["total_outcomes"] == 5                      # a, b, c, f, g
    assert insights["success_rate"] == 0.4
    assert insights["failure_breakdown"] == {"adhesion": 1, "thermal_runaway": 1}
    assert insights["stopped_early"] == {"filament_runout": 1, "power_loss": 1}
    # How a material prints is about the print: the printer's own stops are not PLA's.
    assert insights["material_stats"]["PLA"] == {"count": 3, "success_rate": 0.67}


def test_the_history_rate_readers_see_judged_endings_and_the_person_sees_all(db):
    _six_endings(db)
    assert {r["job_id"] for r in db.list_print_outcomes(printer_name="p1")} == {"a", "b", "c"}
    everything = db.list_print_outcomes(printer_name="p1", include_all=True)
    assert {r["job_id"] for r in everything} == set("abcdefg")


def test_a_versions_machine_record_leaves_out_a_power_cut(db):
    _six_endings(db, version_id="v1", machine_fingerprint=MACHINE)
    entry = db.settled_version_results("v1")[MACHINE]
    # The version on this machine: its own two stops are not the version's.
    assert (entry["completed"], entry["failed"], entry["partial"]) == (2, 1, 0)
    # The machine's recent form: its own stops count against it.
    assert entry["machine_recent"] == {"completed": 2, "failed": 3, "partial": 0}


def test_a_print_certificate_success_rate_leaves_out_a_power_cut(tmp_path, monkeypatch):
    import kiln.persistence as persistence
    from kiln import marketplace_publish

    monkeypatch.setenv("KILN_DB_PATH", str(tmp_path / "kiln.db"))
    monkeypatch.setattr(persistence, "_db", None, raising=False)
    model = tmp_path / "part.stl"
    model.write_bytes(b"solid part\nendsolid part\n")
    db = persistence.get_db()
    try:
        _six_endings(db, file_hash=marketplace_publish._file_hash(str(model)))
        cert = marketplace_publish.generate_print_certificate(str(model))
        assert cert is not None and cert.total_prints == 3
        assert round(cert.success_rate, 2) == 0.67
    finally:
        monkeypatch.setattr(persistence, "_db", None, raising=False)


@pytest.mark.parametrize(
    "mode, by, shared",
    [
        ("power_loss", "user_reported", False),
        ("filament_runout", "inferred", False),
        (None, "observed", False),
        ("other", "inferred", False),
        ("mechanical", "observed", False),
        ("adhesion", "inferred", True),
        (None, "user_reported", True),
    ],
)
def test_only_a_bad_print_joins_the_shared_pool(mode, by, shared):
    from kiln import community_outbox

    with mock.patch.object(community_outbox, "contribute", return_value={"queued": True}) as contribute:
        result = community_outbox.contribute_print_outcome(
            outcome="failed", job_id="job-1", printer_model="A1", material="PLA",
            nozzle_diameter_mm=0.4, extra={"failure_mode": mode}, determined_by=by,
        )
    assert contribute.called is shared
    if not shared:
        assert result == {"contributed": False, "reason": "not_about_the_print"}


def test_a_success_still_joins_the_shared_pool():
    from kiln import community_outbox

    with mock.patch.object(community_outbox, "contribute", return_value={"queued": True}) as contribute:
        community_outbox.contribute_print_outcome(
            outcome="success", job_id="job-2", printer_model="A1", material="PLA", nozzle_diameter_mm=0.4,
        )
    assert contribute.called


def test_the_shared_pool_answer_leaves_out_a_named_stop():
    """Rows already in the pool carry no word for who settled them; a failure
    whose cause names a printer fault or an outside cause is left out."""
    from kiln.community_registry import aggregate_community_records

    rows = [
        {"outcome": "success", "printer_model": "A1", "material": "PLA"},
        {"outcome": "failed", "printer_model": "A1", "material": "PLA", "failure_mode": "warping"},
        {"outcome": "failed", "printer_model": "A1", "material": "PLA", "failure_mode": "power_loss"},
        {"outcome": "failed", "printer_model": "A1", "material": "PLA", "failure_mode": "thermal_runaway"},
        {"outcome": "failed", "printer_model": "A1", "material": "PLA"},
    ]
    insight = aggregate_community_records("sig", rows).to_dict()
    assert insight["total_prints"] == 3
    assert {f["mode"] for f in insight.get("common_failures", [])} == {"warping"}
    assert not fv.pooled_row_is_about_the_print("cancelled", None)


@pytest.mark.parametrize("mode, by, counted", [
    ("power_loss", "user_reported", False),
    (None, "observed", False),
    ("warping", "observed", True),
])
def test_a_decorations_memory_counts_only_a_bad_print(mode, by, counted):
    """A print carrying a decoration that a power cut or the printer stopped
    says nothing about the decoration's settings."""
    from kiln.plugins.learning_tools import record_print_outcome

    with mock.patch("kiln.server._check_auth", return_value=None), \
         mock.patch("kiln.decoration_library.get_decoration") as get_decoration, \
         mock.patch("kiln.decoration_library.record_decoration_failure") as failure, \
         mock.patch("kiln.decoration_library.record_decoration_success"):
        get_decoration.return_value = mock.Mock(proven_settings={})
        record_print_outcome(
            job_id=f"job-deco-{mode}-{by}", outcome="failed", printer_name="p1",
            material_type="PLA", failure_mode=mode, determined_by=by,
            decoration_slug="logo", decoration_settings={"depth_mm": 0.6},
        )
    assert failure.called is counted


# ─────────────────────── no reader judges without the rule ───────────────────

import ast  # noqa: E402
from pathlib import Path  # noqa: E402

SRC = Path(fv.__file__).resolve().parent

#: The calls that ask the rule.
_ASKS = ("ending_verdict", "is_judged_ending", "judged_ending_sql", "failed_ending_kind",
         "pooled_row_is_about_the_print", "_DECIDED", "_MACHINE_DECIDED")

#: Modules that name ending words for another reason, with it.
_NOT_A_JUDGE = {
    "auto_record_hook.py": "records the ending as the printer reports it",
    "cli/main.py": "job status filter choices",
    "cli/output.py": "job status colours",
    "community_sync.py": "the words a shared record may carry; the sender judges first",
    "generation/tripo3d.py": "a model generator's job states",
    "mcp_tasks.py": "task states",
    "model_visualizer.py": "rendered views",
    "plugins/fleet_tools.py": "queue job states",
    "plugins/marketplace_tools.py": "marketplace search results",
    "plugins/queue_tools.py": "queue job states",
    "print_dna.py": "the words a print DNA record may carry",
    "print_recovery.py": "recovery strategy tallies",
    "scheduler.py": "queue job states; a resolved ending is judged where it is shared",
    "traceability.py": "a traceability record's states",
}


def _own_ending_lists(source: str) -> list[int]:
    hits = []
    for node in ast.walk(ast.parse(source)):
        words = None
        if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
            words = {e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}
        elif isinstance(node, ast.Dict):
            words = {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "IN ('success'" in node.value:
            words = {"success", "failed"}
        if words and "failed" in words and words & {"partial", "success", "completed"}:
            hits.append(node.lineno)
    return hits


def test_no_reader_keeps_its_own_list_of_ending_words():
    found = set()
    for path in sorted(SRC.rglob("*.py")):
        source = path.read_text(encoding="utf-8", errors="replace")
        if "failed" in source and _own_ending_lists(source) and not any(a in source for a in _ASKS):
            found.add(path.relative_to(SRC).as_posix())
    assert sorted(found - set(_NOT_A_JUDGE)) == [], "these keep their own ending list and never ask the rule"
    assert sorted(set(_NOT_A_JUDGE) - found) == [], "these no longer keep an ending list; drop them here"


def test_the_scan_can_fail():
    assert _own_ending_lists('_DECIDED = "outcome IN (\'success\', \'failed\', \'partial\')"') == [1]
    assert _own_ending_lists('X = frozenset({"failed", "partial"})') == [1]
    assert _own_ending_lists('X = ("queued", "failed")') == []
