"""A bad print on a printer marked needs repair does not count against the print.

A person marks a printer as needing repair (:mod:`kiln.machine_repairs`).
From that moment until the repair that follows, a bad print on that printer
is the printer's and not the design's: it stays in the person's history under
plain words, it still counts about the printer, and it stops counting about
the design, the material, the settings and the shared pool.  A success is
never set aside, and a repair with no "needs repair" before it sets nothing
aside.  The rule is :func:`kiln.failure_vocabulary.excused_by_repair`; these
tests feed every reader the same prints, through the real repair engine and
the real store.
"""

from __future__ import annotations

import itertools
import sqlite3
import time
from collections import Counter
from types import SimpleNamespace
from unittest import mock

import pytest

from kiln import failure_vocabulary as fv
from kiln import machine_repairs as repairs
from kiln.persistence import KilnDB
from kiln.registry import machine_fingerprint

DAY = 86400.0
NOW = time.time()


def _machine(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=name, serial=f"01P00A0000000{name}", host="192.0.2.42", access_code="code",
        _printer_model="bambu_a1", _kiln_registered_name=name,
        get_state=lambda: SimpleNamespace(state=SimpleNamespace(value="idle")),
    )


A, B = _machine("a"), _machine("b")
FP_A, FP_B = machine_fingerprint(A), machine_fingerprint(B)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    instance = KilnDB(db_path=str(tmp_path / "kiln.db"))
    yield instance
    instance.close()


def _print(db: KilnDB, job: str, outcome: str, *, days_ago: float, machine: str | None = FP_A,
           mode: str | None = "warping", by: str = "user_reported", **extra) -> None:
    db._save_print_outcome({
        "job_id": job, "printer_name": "a", "file_name": f"{job}.gcode", "outcome": outcome,
        "failure_mode": mode if outcome != "success" else None, "determined_by": by,
        "material_type": "PLA", "machine_fingerprint": machine, "version_id": "v1",
        "created_at": NOW - days_ago * DAY, **extra,
    })


def _mark(db: KilnDB, status: str, *, days_ago: float, machine=A, kind: str | None = None) -> None:
    """One mark through the repair engine, dated *days_ago*."""
    repairs.mark(machine, status, kind=kind, when=NOW - days_ago * DAY, db=db)


def _judged(db: KilnDB) -> set[str]:
    return {r["job_id"] for r in db.list_print_outcomes(printer_name="a", limit=500)}


# ─────────────────────────── the rule ────────────────────────────────────────

WINDOWS = {FP_A: [(100.0, 200.0), (500.0, None)]}


@pytest.mark.parametrize("started, excused", [
    (99.9, False), (100.0, True), (150.0, True), (200.0, False), (300.0, False), (500.0, True), (9e9, True),
])
def test_a_bad_print_is_set_aside_only_inside_a_window(started, excused):
    said = dict(machine_fingerprint=FP_A, started_at=started, repair_windows=WINDOWS)
    assert fv.excused_by_repair("failed", "warping", "observed", **said) is excused
    assert fv.is_judged_ending("failed", "warping", "observed", **said) is (not excused)
    # About the machine the print always counts.
    assert fv.is_judged_ending("failed", "warping", "observed", about=fv.ABOUT_THE_MACHINE, **said)
    assert (fv.repair_said("failed", "warping", "observed", **said) == fv.REPAIR_EXCUSED_WORDS) is excused


def test_what_is_never_set_aside():
    inside = dict(machine_fingerprint=FP_A, started_at=150.0, repair_windows=WINDOWS)
    assert not fv.excused_by_repair("success", None, "observed", **inside)
    assert fv.is_judged_ending("success", None, "observed", **inside)
    # A stop is not a bad print: the repair record has nothing to set aside.
    assert not fv.excused_by_repair("failed", "power_loss", "observed", **inside)
    assert not fv.excused_by_repair("cancelled", None, "user_reported", **inside)
    # Another machine, no machine, no start time, no windows.
    assert not fv.excused_by_repair("failed", "warping", "seen", **{**inside, "machine_fingerprint": FP_B})
    assert not fv.excused_by_repair("failed", "warping", "seen", **{**inside, "machine_fingerprint": None})
    assert not fv.excused_by_repair("failed", "warping", "seen", **{**inside, "started_at": None})
    assert not fv.excused_by_repair("failed", "warping", "seen", machine_fingerprint=FP_A, started_at=150.0)
    assert fv.repair_said("success", None, "observed", **inside) is None


def test_the_sql_and_the_python_rule_agree_on_every_row():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE print_outcomes (outcome TEXT, failure_mode TEXT, determined_by TEXT, "
                 "machine_fingerprint TEXT, created_at REAL)")
    combos = list(itertools.product(
        ("success", "failed", "partial", "cancelled", "pending", "unknown"),
        ("warping", "clog", "power_loss", "other", None),
        ("observed", "user_reported", None),
        (FP_A, FP_B, "", None),
        (50.0, 100.0, 199.9, 200.0, 499.0, 500.0, 1e12, None),
    ))
    conn.executemany("INSERT INTO print_outcomes VALUES (?, ?, ?, ?, ?)", combos)
    windows = {**WINDOWS, FP_B: [(499.0, 500.0)]}
    for about in (fv.ABOUT_THE_PRINT, fv.ABOUT_THE_MACHINE):
        want = Counter(
            c for c in combos
            if fv.is_judged_ending(c[0], c[1], c[2], about=about, machine_fingerprint=c[3],
                                   started_at=c[4], repair_windows=windows)
        )
        for alias in ("", "p"):
            got = Counter(conn.execute(
                f"SELECT * FROM print_outcomes {alias} "
                f"WHERE {fv.judged_ending_sql(alias, about=about, repair_windows=windows)}").fetchall())
            assert got == want, (about, alias)
    plain = Counter(c for c in combos if fv.is_judged_ending(c[0], c[1], c[2]))
    assert want == Counter(c for c in combos if fv.is_judged_ending(c[0], c[1], c[2], about=fv.ABOUT_THE_MACHINE))
    assert sum(plain.values()) > sum(Counter(
        c for c in combos
        if fv.is_judged_ending(c[0], c[1], c[2], machine_fingerprint=c[3], started_at=c[4], repair_windows=windows)
    ).values()), "the windows set something aside in this table"
    assert fv.judged_ending_sql(repair_windows={}) == fv.judged_ending_sql()
    with pytest.raises(ValueError):
        fv.judged_ending_sql(repair_windows={"x' OR '1'='1": [(1.0, None)]})


# ─────────────────────────── the windows, off the real record ────────────────

def test_a_needs_repair_mark_opens_a_window_and_the_repair_closes_it(db):
    _mark(db, "needed", days_ago=10)
    assert db.repair_windows() == {FP_A: [(pytest.approx(NOW - 10 * DAY), None)]}
    _mark(db, "done", days_ago=4, kind="belts")
    assert db.repair_windows() == {FP_A: [(pytest.approx(NOW - 10 * DAY), pytest.approx(NOW - 4 * DAY))]}


def test_a_repair_with_no_needs_repair_before_it_opens_nothing(db):
    """A nozzle swap is recorded as a repair with no mark before it."""
    _print(db, "before-swap", "failed", days_ago=6)
    _mark(db, "done", days_ago=3, kind="nozzle")
    assert db.repair_windows() == {}
    assert "before-swap" in _judged(db)
    # The machine's own fresh start is as it was.
    assert db.machine_condition(FP_A)["repaired_at"] == pytest.approx(NOW - 3 * DAY)
    # A later mark opens its own window and reaches back to nothing.
    _mark(db, "needed", days_ago=1)
    assert db.repair_windows() == {FP_A: [(pytest.approx(NOW - 1 * DAY), None)]}
    assert "before-swap" in _judged(db)


def test_a_mark_the_person_took_back_opens_nothing(db):
    _print(db, "during", "failed", days_ago=2)
    _mark(db, "needed", days_ago=5)
    assert "during" not in _judged(db)
    repairs.mark(A, "clear", db=db)
    assert db.repair_windows() == {}
    assert "during" in _judged(db)


# ─────────────────────────── the readers ─────────────────────────────────────

def _a_broken_fortnight(db: KilnDB) -> None:
    """Marked needs repair ten days ago, repaired four days ago."""
    _print(db, "good-before", "success", days_ago=20)
    _print(db, "bad-before", "failed", days_ago=12)          # before the mark: the design's
    _mark(db, "needed", days_ago=10)
    _print(db, "bad-during", "failed", days_ago=8)           # the printer's
    _print(db, "bad-during-unnamed", "failed", days_ago=7, mode=None)  # the person saw it bad
    _print(db, "good-during", "success", days_ago=6)         # it came out: it counts
    _mark(db, "done", days_ago=4, kind="belts")
    _print(db, "bad-after", "partial", days_ago=2)           # after the repair: the design's
    _print(db, "bad-other-machine", "failed", days_ago=8, machine=FP_B)
    _print(db, "bad-no-machine", "failed", days_ago=8, machine=None)


def test_the_judged_list_leaves_out_only_the_bad_prints_inside_the_window(db):
    _a_broken_fortnight(db)
    everything = {r["job_id"] for r in db.list_print_outcomes(printer_name="a", include_all=True, limit=500)}
    assert _judged(db) == everything - {"bad-during", "bad-during-unnamed"}
    # Still in the person's history, and still the printer's own record.
    assert {"bad-during", "bad-during-unnamed"} <= everything
    kept = {r["job_id"] for r in db.list_print_outcomes(printer_name="a", count_repair_excused=True, limit=500)}
    assert kept == everything


def test_a_materials_rate_leaves_them_out_and_the_printers_own_rate_keeps_them(db):
    _a_broken_fortnight(db)
    insights = db.get_printer_learning_insights("a")
    # The printer: every judged ending, the two it made while broken included.
    assert insights["total_outcomes"] == 8
    assert insights["success_rate"] == 0.25
    assert insights["failure_breakdown"] == {"warping": 4}
    # PLA: two successes and the four bad prints that are about the print.
    assert insights["material_stats"]["PLA"] == {"count": 6, "success_rate": 0.33}


def test_a_printers_trend_keeps_them_and_its_material_figures_do_not(db):
    from kiln.print_trend_analysis import analyze_printer_trends

    _a_broken_fortnight(db)
    report = analyze_printer_trends("a", db=db, lookback_days=30)
    assert report.total_prints == 8                      # the printer's own record, whole
    material = report.to_dict()["material_reliability"]["PLA"]
    assert material == {"total": 6, "success_rate": 0.33}


def test_a_print_certificate_leaves_them_out(tmp_path, monkeypatch):
    import kiln.persistence as persistence
    from kiln import marketplace_publish

    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    monkeypatch.setenv("KILN_DB_PATH", str(tmp_path / "kiln.db"))
    monkeypatch.setattr(persistence, "_db", None, raising=False)
    model = tmp_path / "part.stl"
    model.write_bytes(b"solid part\nendsolid part\n")
    db = persistence.get_db()
    try:
        fhash = marketplace_publish._file_hash(str(model))
        _print(db, "good", "success", days_ago=20, file_hash=fhash)
        _mark(db, "needed", days_ago=10)
        _print(db, "bad-during", "failed", days_ago=8, file_hash=fhash)
        cert = marketplace_publish.generate_print_certificate(str(model))
        assert cert is not None and (cert.total_prints, cert.success_rate) == (1, 1.0)
        repairs.mark(A, "clear", db=db)
        cert = marketplace_publish.generate_print_certificate(str(model))
        assert (cert.total_prints, cert.success_rate) == (2, 0.5)
    finally:
        monkeypatch.setattr(persistence, "_db", None, raising=False)


@pytest.fixture
def live_db(tmp_path, monkeypatch):
    """The store the tools and the pool door open for themselves."""
    import kiln.persistence as persistence

    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    monkeypatch.setenv("KILN_DB_PATH", str(tmp_path / "kiln.db"))
    monkeypatch.setattr(persistence, "_db", None, raising=False)
    pro_hook = None
    try:
        from kiln_pro.print_learning import auto_record as pro_hook

        installed = pro_hook.uninstall_auto_record_hook()
    except ImportError:
        installed = False
    import kiln.server as server

    monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
    yield persistence.get_db()
    if installed and pro_hook is not None:
        pro_hook.install_auto_record_hook()
    monkeypatch.setattr(persistence, "_db", None, raising=False)


def _contribute(job: str, *, outcome: str = "failed", mode: str | None = "warping") -> tuple[bool, dict]:
    from kiln import community_outbox

    with mock.patch.object(community_outbox, "contribute", return_value={"queued": True}) as contribute:
        result = community_outbox.contribute_print_outcome(
            outcome=outcome, job_id=job, printer_model="A1", material="PLA", nozzle_diameter_mm=0.4,
            extra={"failure_mode": mode}, determined_by="user_reported",
        )
    return contribute.called, result


def test_the_shared_pool_takes_no_bad_print_from_a_printer_marked_needs_repair(live_db):
    live_db.open_pending_outcome("start:a:1", "a", "bracket.gcode", identity={"machine_fingerprint": FP_A})
    assert _contribute("bracket")[0] is True                     # nothing marked: shared
    _mark(live_db, "needed", days_ago=1)
    live_db.open_pending_outcome("start:a:2", "a", "bracket.gcode", identity={"machine_fingerprint": FP_A})
    called, result = _contribute("bracket")
    assert called is False and result == {"contributed": False, "reason": "printer_marked_for_repair"}
    assert _contribute("bracket", outcome="success", mode=None)[0] is True   # a success is shared
    # A print on another printer is not this printer's.
    live_db.open_pending_outcome("start:b:1", "b", "gasket.gcode", identity={"machine_fingerprint": FP_B})
    assert _contribute("gasket")[0] is True


def test_a_decorations_memory_does_not_count_it(live_db):
    from kiln.plugins.learning_tools import record_print_outcome

    def record(job: str) -> bool:
        live_db.open_pending_outcome(job, "a", f"{job}.gcode", identity={"machine_fingerprint": FP_A})
        with mock.patch("kiln.decoration_library.get_decoration") as get_decoration, \
             mock.patch("kiln.decoration_library.record_decoration_failure") as failure, \
             mock.patch("kiln.decoration_library.record_decoration_success"):
            get_decoration.return_value = mock.Mock(proven_settings={})
            record_print_outcome(
                job_id=job, outcome="failed", printer_name="a", material_type="PLA",
                failure_mode="warping", determined_by="user_reported",
                decoration_slug="logo", decoration_settings={"depth_mm": 0.6},
            )
        return failure.called

    assert record("job-before") is True
    _mark(live_db, "needed", days_ago=0.5)
    assert record("job-during") is False


def test_the_persons_history_says_it_in_plain_words(live_db):
    from kiln.server import mcp

    _a_broken_fortnight(live_db)
    out = mcp._tool_manager.get_tool("get_printer_insights").fn(printer_name="a", limit=50)
    said = {r["job_id"]: r.get("repair_said") for r in out["recent_outcomes"]}
    assert said["bad-during"] == fv.REPAIR_EXCUSED_WORDS == said["bad-during-unnamed"]
    assert {job for job, words in said.items() if words} == {"bad-during", "bad-during-unnamed"}
    assert "needs repair" in fv.REPAIR_EXCUSED_WORDS and "design" in fv.REPAIR_EXCUSED_WORDS


# ─────────────────────── no reader judges a print without the record ─────────

import ast  # noqa: E402
from pathlib import Path  # noqa: E402

SRC = Path(fv.__file__).resolve().parent

#: Calls that judge an ending about the print without the repair windows,
#: by module: how many, and what each is.
_WITHOUT_THE_RECORD = {
    "persistence.py": (2, "the rule before the repair record (_DECIDED, kept for the printer's own "
                          "record), and a version's results per machine, which routing ages by the "
                          "machine's own repair line"),
}


def _judges_without_windows(source: str) -> tuple[int, int]:
    """``(calls, reads)``: calls of the rule about the print that hand in no
    repair windows, and reads of ``_DECIDED`` off a store."""
    calls = reads = 0
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            keywords = {k.arg for k in node.keywords}
            if name in ("is_judged_ending", "judged_ending_sql") and not keywords & {"about", "repair_windows"} \
                    and not any(k.arg is None for k in node.keywords):
                calls += 1
        elif isinstance(node, ast.Attribute) and node.attr == "_DECIDED":
            reads += 1
    return calls, reads


def test_no_reader_judges_a_print_without_the_repair_record():
    found = {}
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "failure_vocabulary.py":
            continue
        calls, reads = _judges_without_windows(path.read_text(encoding="utf-8", errors="replace"))
        rel = path.relative_to(SRC).as_posix()
        if rel != "persistence.py":
            calls += reads
        if calls:
            found[rel] = calls
    assert found == {rel: count for rel, (count, _why) in _WITHOUT_THE_RECORD.items()}, (
        "a reader judging a print asks KilnDB.decided_sql(), or hands the row's machine, its start "
        "and KilnDB.repair_windows() to is_judged_ending")


def test_the_scan_can_fail():
    assert _judges_without_windows("x = is_judged_ending(o, m, b)") == (1, 0)
    assert _judges_without_windows("x = fv.judged_ending_sql('p')") == (1, 0)
    assert _judges_without_windows("x = f'... {db._DECIDED}'") == (0, 1)
    assert _judges_without_windows("x = is_judged_ending(o, m, b, **row_and_windows)") == (0, 0)
    assert _judges_without_windows("x = judged_ending_sql(about=ABOUT_THE_MACHINE)") == (0, 0)
    assert _judges_without_windows("x = judged_ending_sql(a, repair_windows=w)") == (0, 0)
