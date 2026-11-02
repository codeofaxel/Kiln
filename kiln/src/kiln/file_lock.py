"""One cross-platform exclusive file lock for every Kiln process on a machine.

Several Kiln stores are read and rewritten by more than one process at once
(the MCP server, the bridge daemon, the CLI, kiln-pro's REST API), so each
guards its file with an advisory lock taken on an open descriptor.  Every
one of those sites used to spell the lock out itself with ``fcntl.flock``,
and Windows has no ``fcntl``: the lazy imports there silently held nothing,
and the one module-level import stopped a whole kiln-pro plugin from loading
on every Windows run.  This module owns the mechanics once so a caller can
never again pick a primitive its platform lacks.

POSIX takes ``fcntl.flock`` and blocks until the holder lets go.  Windows
takes an ``msvcrt.locking`` byte-range lock on the first byte instead;
``msvcrt`` cannot block properly on its own (``LK_LOCK`` retries once a
second and gives up after ten), so the non-blocking form is polled, which
also lets a caller bound how long it will wait.  Byte-range locks are
mandatory on Windows -- another handle reading a locked byte fails -- so
callers lock a sibling ``.lock`` file rather than the data file they are
about to read.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import time
from collections.abc import Iterator
from typing import IO

logger = logging.getLogger(__name__)

# How often a Windows waiter re-tries the non-blocking take.  Fine-grained
# enough that it is through within milliseconds of the holder's release,
# coarse enough not to spin a core while it waits.
_WINDOWS_POLL_S = 0.02

# How long a Windows waiter polls before going ahead without the lock, for
# the stores that would rather run their rare race than stall every Kiln
# process behind a wedged holder: comfortably past one of their critical
# sections, and bounded.  POSIX ``flock`` has no such bound.
DEFAULT_WINDOWS_WAIT_S = 15.0

# ``msvcrt.locking`` reports a byte another handle holds as EACCES, and a
# wait it judged deadlocked as EDEADLOCK; both mean "try again".  Any other
# error (a closed or unseekable descriptor, say) will not clear with time.
_WINDOWS_BUSY_ERRNOS = frozenset(
    {errno.EACCES, getattr(errno, "EDEADLOCK", 36), getattr(errno, "EDEADLK", 36)}
)


def _descriptor(fh: int | IO) -> int:
    return fh if isinstance(fh, int) else fh.fileno()


@contextlib.contextmanager
def exclusive_file_lock(fh: int | IO, *, timeout: float | None = None) -> Iterator[bool]:
    """Hold an exclusive advisory lock on the open file *fh* for the block.

    *fh* is a descriptor or a file object; the lock is released when the block
    ends, even on an exception, and the caller still owns closing the file.
    The block receives ``True`` when the lock is held and ``False`` when it
    could not be taken -- a filesystem without lock support, a platform with
    neither primitive, or a Windows wait past *timeout* seconds.  The lock
    is advisory everywhere Kiln runs, so a lock that cannot be taken degrades
    to running unlocked rather than refusing the caller's work; each store
    already tolerates the rare race that leaves.

    *timeout* bounds the wait on Windows only.  ``None`` waits as long as
    POSIX ``flock`` would, which is until the holder releases.

    Two cautions for callers.  Taking the lock twice through one handle is
    a no-op under ``flock`` but refused on Windows, so hold it once per
    handle.  And the Windows take rewinds the raw descriptor beneath any
    Python buffer, so pass a handle nothing writes through -- the empty
    sibling ``.lock`` file every Kiln store already uses.
    """
    fd = _descriptor(fh)
    try:
        import fcntl
    except ImportError:
        fcntl = None

    if fcntl is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError:
            logger.debug("exclusive_file_lock: flock not taken on fd %s", fd, exc_info=True)
            yield False
            return
        try:
            yield True
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        return

    try:
        import msvcrt
    except ImportError:
        yield False
        return

    if not _lock_windows(fd, msvcrt, timeout):
        logger.debug("exclusive_file_lock: msvcrt lock not taken on fd %s", fd)
        yield False
        return
    try:
        yield True
    finally:
        with contextlib.suppress(OSError):
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def _lock_windows(fd: int, msvcrt, timeout: float | None) -> bool:
    """Take the first byte of *fd* exclusively; ``False`` once *timeout* passes.

    The region starts at the file position, so the descriptor is rewound
    first: two processes that opened the same file must lock the same byte
    for the locks to exclude each other, and a descriptor opened for append
    may sit anywhere.  Locking a byte past the end of an empty file is
    allowed, which is what makes an empty sibling ``.lock`` file work.  Locks
    are per handle on Windows, so separate descriptors -- separate processes,
    or threads holding their own -- exclude each other.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError as exc:
            if exc.errno not in _WINDOWS_BUSY_ERRNOS:
                logger.debug("exclusive_file_lock: msvcrt refused fd %s", fd, exc_info=True)
                return False
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(_WINDOWS_POLL_S)
