"""One answer to "is process *pid* still running?" for every Kiln process.

Several stores remember the pid of the process that holds something -- the
printer reservation, a cool-down marker, the bridge daemon's state file, the
ingest service's pidfile, the ending-watch pidfile -- and each decides what a
dead holder means for itself.  Every one of them used to ask the question in
its own words, and on Windows most of them could not ask at all: the POSIX
idiom ``os.kill(pid, 0)`` is not a probe there (signal 0 is ``CTRL_C_EVENT``
and any other signal is ``TerminateProcess``), so three sites answered
"alive" unconditionally and a dead holder kept the printer until its
reservation ran out, thirty minutes later.  Two sites carried a correct
``OpenProcess`` probe, one of them reading access-denied as dead.  This
module owns the mechanics once so a caller can never again pick a primitive
its platform lacks, in the same spirit as :mod:`kiln.file_lock`.

The bound, on every platform: a pid that was reused by another program reads
as alive.  Callers must therefore keep their own age limit on whatever a pid
is holding; this answers only "does a process with this number exist".
"""

from __future__ import annotations

import os
from typing import Any

# Windows constants, named here so the two system calls below read as what
# they are.  PROCESS_QUERY_LIMITED_INFORMATION is the least a handle can ask
# for and is granted on processes we could never signal or read.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_PARAMETER = 87


def pid_alive(pid: Any, *, unknown: bool = True) -> bool:
    """Whether a process with number *pid* exists right now.

    Anything that is not a positive ``int`` (``None``, a string, a ``bool``,
    zero, a negative number) is ``False``: there is no such process to ask
    about.  Our own pid is ``True`` without asking.

    ``unknown`` is the answer when the operating system cannot say -- an
    ``OpenProcess`` failure Windows does not explain, an ``OSError`` POSIX
    does not classify.  A caller that would rather keep a hold it cannot
    verify passes ``True`` (the printer reservation: "every uncertainty
    allows"); a caller that would otherwise refuse to start forever on a
    stale pidfile passes ``False``.  Access denied is never unknown: a
    process we may not open or signal is a process.

    The bound: a pid reused by another program reads as alive, so a caller
    keeps its own age limit on whatever the pid holds.

    Never ``os.kill`` on Windows.  There, ``os.kill(pid, 0)`` sends
    ``CTRL_C_EVENT`` and any other signal is ``TerminateProcess``, so the
    POSIX probe would interrupt or end the process it meant only to ask
    about.  Windows is asked through a query-only process handle instead.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        return _windows_alive(pid, unknown)
    return _posix_alive(pid, unknown)


def _posix_alive(pid: int, unknown: bool) -> bool:
    """Signal 0 delivers nothing on POSIX; it only asks whether *pid* exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return unknown
    return True


def _kernel32():
    """The Windows kernel library, opened so ``GetLastError`` is kept per
    call, with the three calls' signatures declared: without them ctypes
    passes and returns C ints, which truncates a 64-bit handle."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel32


def _last_error() -> int:
    import ctypes

    return ctypes.get_last_error()


def _windows_alive(pid: int, unknown: bool) -> bool:
    """Open *pid* for nothing but a status query and read its exit code.

    A handle that opens but whose exit code is not ``STILL_ACTIVE`` is a
    process that has ended and is only kept in the table by someone else's
    open handle: dead for every caller here.  Microsoft's own caveat holds:
    a process that exited WITH code 259 reads alive, which the pid-reuse
    bound already covers (a caller keeps its own age limit).
    """
    import ctypes

    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        error = _last_error()
        if error == _ERROR_ACCESS_DENIED:
            return True  # exists, but not ours to open
        if error == _ERROR_INVALID_PARAMETER:
            return False  # no process has this number
        return unknown
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return unknown
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


__all__ = ["pid_alive"]
