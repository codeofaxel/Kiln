"""``kiln.file_lock.exclusive_file_lock`` holds, releases, and degrades.

The lock is the one primitive every Kiln store shares across processes, so
these prove the contract on whichever platform runs the suite: a second
descriptor cannot take the lock while the block holds it, the lock is given
back when the block ends (even by an exception), and a platform with neither
``fcntl`` nor ``msvcrt`` -- or a Windows wait past its bound -- runs the
block unlocked and says so, instead of refusing the caller's work.
"""

from __future__ import annotations

import builtins
import os
import sys
import types

import pytest

from kiln.file_lock import exclusive_file_lock


def _second_take_fails(path) -> bool:
    """Whether a fresh descriptor is refused a non-blocking take on *path*,
    which is what another process would see while the lock is held."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        except ImportError:
            import msvcrt

            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            return False
    finally:
        os.close(fd)


@pytest.fixture
def lock_path(tmp_path):
    return tmp_path / "store.lock"


def test_second_descriptor_is_refused_while_the_lock_is_held(lock_path):
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with exclusive_file_lock(fd) as held:
            assert held is True
            assert _second_take_fails(lock_path)
    finally:
        os.close(fd)


def test_lock_is_released_when_the_block_ends(lock_path):
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with exclusive_file_lock(fd):
            pass
        assert not _second_take_fails(lock_path)
    finally:
        os.close(fd)


def test_lock_is_released_when_the_block_raises(lock_path):
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with pytest.raises(RuntimeError), exclusive_file_lock(fd):
            raise RuntimeError("the store's own failure")
        assert not _second_take_fails(lock_path)
    finally:
        os.close(fd)


def test_a_file_object_is_accepted_in_place_of_a_descriptor(lock_path):
    """Two of the stores hold a ``with open(...)`` handle rather than a raw
    descriptor; the helper locks through its ``fileno()``."""
    with open(lock_path, "a", encoding="utf-8") as handle, exclusive_file_lock(handle) as held:
        assert held is True
        assert _second_take_fails(lock_path)
    assert not _second_take_fails(lock_path)


def test_an_appending_descriptor_locks_the_same_byte_as_a_fresh_one(lock_path):
    """On Windows the lock is a byte range at the file position, so a
    descriptor that has written -- and now sits past the start -- must still
    exclude a fresh descriptor at position zero.  The helper rewinds; this
    holds on POSIX trivially because ``flock`` is whole-file."""
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_APPEND, 0o600)
    try:
        os.write(fd, b"a few bytes already here\n")
        with exclusive_file_lock(fd):
            assert _second_take_fails(lock_path)
    finally:
        os.close(fd)


def _without_modules(monkeypatch, *names):
    """Make ``import <name>`` fail for each of *names*, as a platform that
    lacks the module would, and drop any cached copy so the import is retried."""
    real_import = builtins.__import__

    def refusing(name, *args, **kwargs):
        if name in names:
            raise ImportError(f"no {name} on this platform")
        return real_import(name, *args, **kwargs)

    for name in names:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(builtins, "__import__", refusing)


def test_without_fcntl_the_lock_goes_through_msvcrt(lock_path, monkeypatch):
    """Runs anywhere: with ``fcntl`` missing, as on Windows, the lock is
    taken (and given back) through ``msvcrt`` rather than silently skipped."""
    calls: list[int] = []
    fake = types.SimpleNamespace(
        LK_NBLCK=2, LK_UNLCK=0, locking=lambda fd, mode, n: calls.append(mode)
    )
    _without_modules(monkeypatch, "fcntl")
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with exclusive_file_lock(fd) as held:
            assert held is True
            assert calls == [fake.LK_NBLCK]
        assert calls == [fake.LK_NBLCK, fake.LK_UNLCK]
    finally:
        os.close(fd)


def test_with_neither_primitive_the_block_runs_unlocked(lock_path, monkeypatch):
    _without_modules(monkeypatch, "fcntl", "msvcrt")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with exclusive_file_lock(fd) as held:
            assert held is False
    finally:
        os.close(fd)


def test_a_windows_error_that_waiting_cannot_clear_is_not_retried(lock_path, monkeypatch):
    """Only a busy byte is worth polling; a bad descriptor would otherwise
    spin an unbounded waiter forever."""
    import errno

    calls: list[int] = []

    def bad_descriptor(fd, mode, n):
        calls.append(mode)
        raise OSError(errno.EBADF, "bad file descriptor")

    fake = types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=bad_descriptor)
    _without_modules(monkeypatch, "fcntl")
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with exclusive_file_lock(fd, timeout=None) as held:
            assert held is False
    finally:
        os.close(fd)
    assert calls == [fake.LK_NBLCK]


def test_a_windows_waiter_gives_up_after_its_timeout(lock_path, monkeypatch):
    """A holder that never lets go must not wedge every waiter forever when
    the caller bounded the wait; the block then runs unlocked and is told
    so, and nothing is unlocked that was never locked."""
    calls: list[int] = []

    def always_held(fd, mode, n):
        calls.append(mode)
        raise OSError(13, "locked by another process")

    fake = types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=always_held)
    _without_modules(monkeypatch, "fcntl")
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    monkeypatch.setattr("kiln.file_lock._WINDOWS_POLL_S", 0.001)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with exclusive_file_lock(fd, timeout=0.05) as held:
            assert held is False
    finally:
        os.close(fd)
    assert calls and all(mode == fake.LK_NBLCK for mode in calls)
