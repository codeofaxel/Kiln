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
