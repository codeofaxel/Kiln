"""The suite must be physically unable to write the user's real database.

A developer's ~/.kiln/kiln.db accumulated 1,811 phantom prints, 462 jobs and
333 outcomes from test files, against exactly ONE genuine print — so the
print history read as a busy shop that had never happened. daily_stats was
given a write-side guard for this class; the database underneath it was not.
"""

from __future__ import annotations

import os
from pathlib import Path


def test_the_default_path_is_refused_under_a_test_runner():
    from kiln import persistence

    redirected = persistence._redirect_if_test_runner(
        persistence._DEFAULT_DB_PATH
    )
    assert redirected != persistence._DEFAULT_DB_PATH
    assert str(Path.home() / ".kiln" / "kiln.db") not in redirected


def test_an_explicit_path_is_never_redirected(tmp_path):
    """A test that points KILN_DB_PATH at its own file wants persistence."""
    from kiln import persistence

    mine = str(tmp_path / "mine.db")
    assert persistence._redirect_if_test_runner(mine) == mine


def test_a_bare_KilnDB_does_not_touch_the_real_file(monkeypatch, tmp_path):
    """The shape that caused it: KilnDB() with no argument."""
    from kiln.persistence import KilnDB, _DEFAULT_DB_PATH

    monkeypatch.delenv("KILN_DB_PATH", raising=False)
    db = KilnDB()
    try:
        assert db._db_path != _DEFAULT_DB_PATH
    finally:
        try:
            db.close()
        except Exception:
            pass


def test_the_conftest_fixture_points_somewhere_temporary():
    """Suspenders: even without the belt, the env var is redirected."""
    path = os.environ.get("KILN_DB_PATH", "")
    assert path
    assert str(Path.home() / ".kiln" / "kiln.db") != path


class TestTheSingletonAnswersForThePathInForce:
    """``get_db()`` hands out ONE instance, and that instance must be the
    database the environment names now.  A leaked background thread from an
    earlier test can begin building the singleton just before a test binds
    ``KILN_DB_PATH`` to its own file; the build finishes after, the test's
    reset of the singleton lands on a still-empty slot, and ``get_db()``
    then answers with a database bound to the OLD path -- the test writes a
    row it can never read back.  Seen on one CI worker in four."""

    def test_a_singleton_bound_elsewhere_is_rebuilt_for_the_new_path(self, monkeypatch, tmp_path):
        from kiln import persistence

        old = persistence.KilnDB(db_path=str(tmp_path / "old.db"))
        monkeypatch.setattr(persistence, "_db", old)
        monkeypatch.setenv("KILN_DB_PATH", str(tmp_path / "new.db"))
        try:
            db = persistence.get_db()
            assert db is not old
            assert db._db_path == str(tmp_path / "new.db")
            assert persistence.get_db() is db  # and it is the singleton from here on
        finally:
            old.close()
            persistence.get_db().close()

    def test_a_build_that_started_before_the_path_changed_does_not_win(self, monkeypatch, tmp_path):
        import threading

        from kiln import persistence

        started, go = threading.Event(), threading.Event()
        real = persistence.KilnDB

        class BuildInFlight(real):
            """Reads the path, then waits -- a build the environment overtakes."""

            def __init__(self, *args, **kwargs):
                path = os.environ["KILN_DB_PATH"]
                started.set()
                assert go.wait(5.0)
                super().__init__(path, *args, **kwargs)

        monkeypatch.setenv("KILN_DB_PATH", str(tmp_path / "before.db"))
        monkeypatch.setattr(persistence, "_db", None)
        monkeypatch.setattr(persistence, "KilnDB", BuildInFlight)
        leaked = threading.Thread(target=persistence.get_db, name="leaked-from-an-earlier-test")
        leaked.start()
        assert started.wait(5.0)
        # The test binds its own file and resets the slot, exactly as the
        # conftest fixture and the older-database test do.
        monkeypatch.setenv("KILN_DB_PATH", str(tmp_path / "mine.db"))
        monkeypatch.setattr(persistence, "_db", None)
        monkeypatch.setattr(persistence, "KilnDB", real)
        go.set()
        leaked.join(5.0)
        try:
            assert persistence.get_db()._db_path == str(tmp_path / "mine.db")
        finally:
            persistence.get_db().close()
