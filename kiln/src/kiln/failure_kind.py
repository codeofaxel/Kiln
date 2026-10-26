"""Whether a failure can go differently next time, read from what was raised.

Every error envelope Kiln returns carries ``retryable``.  An agent reads it to
decide whether to call again, so a wrong ``True`` sends it round a loop and a
wrong ``False`` makes it give up on a printer that was only busy.  The code a
door passes says little: most doors wrap whatever went wrong as the generic
``ERROR`` or ``INTERNAL_ERROR``, and both used to mean "retry", so a missing
file, a file that is not a model and a bad argument all told the agent to try
the same call again.

What was raised says more, and it is in hand wherever an envelope is built
inside an ``except`` block, which is where nearly all of them are built.  This
module is the one reading of it, asked by :func:`kiln.server._error_dict`:

* :func:`retryable` -- ``True`` for a failure that depends on something outside
  the call (the network, a timeout, a busy device), ``False`` for one that the
  same input reproduces (no such file, a file that is not a model, a value the
  call cannot use), ``None`` when the exception says neither, so the code's own
  default stands.
* :func:`refusal_code` -- the code a caller's input failure is answered with
  when a door labelled it ``INTERNAL_ERROR``: it was not Kiln's fault, and an
  agent that reads ``INTERNAL_ERROR`` waits for a fix that is not coming.

A cause the exception names (``raise ... from``, a ``PrinterError``'s
``cause``) is followed when the outer exception says neither, so an adapter
that re-wraps a timeout is still read as a timeout.
"""

from __future__ import annotations

import errno
import struct
import zipfile
from collections.abc import Iterator

#: OS error numbers that describe the moment, not the input: try again later.
_TRANSIENT_ERRNOS = frozenset(
    getattr(errno, name)
    for name in (
        "EAGAIN", "EBUSY", "ETIMEDOUT", "ECONNREFUSED", "ECONNRESET", "ECONNABORTED",
        "ENETUNREACH", "ENETDOWN", "EHOSTUNREACH", "EHOSTDOWN", "EPIPE", "EINTR",
    )
    if hasattr(errno, name)
)

#: Libraries whose own exceptions are about a network conversation.
_NETWORK_LIBRARIES = ("requests", "httpx", "httpcore", "urllib3", "aiohttp", "paho", "websocket")

#: Named by what the file system said: the path the caller gave is the problem.
_NOT_THERE: tuple[type[BaseException], ...] = (FileNotFoundError, NotADirectoryError, IsADirectoryError)
_PATH_ERRORS: tuple[type[BaseException], ...] = (*_NOT_THERE, PermissionError, FileExistsError)

#: Raised for a value the call cannot use.  The same value fails the same way.
_VALUE_ERRORS: tuple[type[BaseException], ...] = (
    ValueError, TypeError, zipfile.BadZipFile, struct.error,
)


def _chain(exc: BaseException | None) -> Iterator[BaseException]:
    seen: set[int] = set()
    while isinstance(exc, BaseException) and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or getattr(exc, "cause", None)


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (ConnectionError, TimeoutError, BlockingIOError, InterruptedError)):
        return True
    if type(exc).__name__ in ("TimeoutExpired", "Timeout", "ReadTimeout", "ConnectTimeout"):
        return True
    if isinstance(exc, OSError) and not isinstance(exc, _PATH_ERRORS) and exc.errno in _TRANSIENT_ERRNOS:
        return True
    return any(
        (getattr(cls, "__module__", "") or "").split(".")[0] in _NETWORK_LIBRARIES
        for cls in type(exc).__mro__
    )


def _is_caller_input(exc: BaseException) -> bool:
    return isinstance(exc, _PATH_ERRORS + _VALUE_ERRORS)


def retryable(exc: BaseException | None) -> bool | None:
    """``True`` when *exc* says the failure may not happen again, ``False``
    when the same call will fail the same way, ``None`` when it says neither."""
    for link in _chain(exc):
        if _is_transient(link):
            return True
        if _is_caller_input(link):
            return False
    return None


def refusal_code(exc: BaseException | None) -> str | None:
    """The code for a caller's input failure, or ``None`` for anything else.

    Only the failures that are plainly about what the caller handed in: a path
    the file system refused, and a file Kiln's readers could not read as a
    model (:class:`kiln.mesh_errors.UnreadableMeshError`, which a mesh too
    large to hold and a path with no file at it are too).  A bare
    ``ValueError`` is left alone: it is not retryable, but it can as well be
    Kiln's own mistake.
    """
    from kiln.mesh_errors import MeshFileMissing, UnreadableMeshError

    for link in _chain(exc):
        if _is_transient(link):
            return None
        if isinstance(link, PermissionError):
            return "PERMISSION_DENIED"
        if isinstance(link, (*_NOT_THERE, MeshFileMissing)):
            return "FILE_NOT_FOUND"
        if isinstance(link, UnreadableMeshError):
            return "UNREADABLE_INPUT"
    return None
