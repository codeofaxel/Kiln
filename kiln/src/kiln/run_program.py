"""Start another program from Kiln: every child process goes through here.

On macOS a child is started with ``posix_spawn``, never ``fork()`` followed
by ``exec``.  After a ``fork()``, code that macOS system frameworks register
with ``pthread_atfork`` runs inside the child before the new program starts,
and in a long-running, multi-threaded server that code can crash the child
(signal 11) before the program ever runs: the slicer, OpenSCAD and the print
approval notification then never start, and every retry dies the same way.
``posix_spawn`` is a system call on macOS, so no such code runs in the child.

Python's :mod:`subprocess` already starts a child with ``posix_spawn`` when a
call meets its conditions; on macOS a default call misses them, chiefly
because Python cannot close inherited descriptors through ``posix_spawn``
there.  :func:`_prepare` meets them without changing what the child sees:

* ``close_fds=False``.  Every descriptor Python opens is non-inheritable
  (PEP 446) and closes when the program starts, so the only thing this lets
  through is a descriptor something deliberately made inheritable.
* The program is resolved to a full path first, on the ``PATH`` the child
  will have (``posix_spawn`` takes a path).  A program that is not there
  raises :class:`FileNotFoundError`, as :mod:`subprocess` does.
* A working directory or a new session, which Python's ``posix_spawn`` path
  cannot set, is set by a short trampoline: this Python, started isolated,
  changes directory and/or starts the session, puts back the signals Python
  ignores at startup, and replaces itself with the program, so the process
  id is the program's.
* A child stream that would be one of this process's own descriptors 0-2
  is handed over as a copy numbered above 2 (Python's ``posix_spawn`` path
  refuses the low ones) and closed again once the child has started.

A call carrying an option ``posix_spawn`` cannot honour at all
(``preexec_fn``, ``pass_fds``, a user, group or umask, a process group) goes
to :mod:`subprocess` exactly as given; no launch in Kiln uses one.  On every
other platform every call goes to :mod:`subprocess` unchanged.

The functions mirror :mod:`subprocess` (``run``, ``Popen``,
``check_output``, ``call``) and keep its constants and exceptions; a result,
a process or an exception names the command the caller gave, never the
trampoline.  ``tests/test_run_program.py`` pins that no launch through here
forks, and that every launch in Kiln goes through here.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

#: Options with no ``posix_spawn`` equivalent: a call carrying one is left
#: exactly as given.
_FORK_ONLY_OPTIONS = ("preexec_fn", "pass_fds", "user", "group", "extra_groups", "umask", "process_group")

#: Changes directory and/or starts a session, then becomes the program.
#: Arguments after ``-c``: new-session flag, signal-restore flag, working
#: directory (empty for none), program path, then the program's own argv.
_TRAMPOLINE = (
    "import os,signal,sys\n"
    "a=sys.argv\n"
    "if a[2]=='1':\n"
    " for n in ('SIGPIPE','SIGXFZ','SIGXFSZ'):\n"
    "  s=getattr(signal,n,None)\n"
    "  if s is not None:signal.signal(s,signal.SIG_DFL)\n"
    "if a[1]=='1':os.setsid()\n"
    "if a[3]:os.chdir(a[3])\n"
    "os.execv(a[4],a[5:])\n"
)


#: What carries the command it ran (``.args``), held as the classes
#: themselves: a caller may swap ``subprocess.Popen`` for a stand-in, and a
#: stand-in is not a type to test against.
_CARRIES_ARGS = (subprocess.CompletedProcess, subprocess.Popen)


def _avoids_fork() -> bool:
    """Whether a launch here must keep off ``fork()``: macOS only."""
    return sys.platform == "darwin"


def _set(value: Any) -> bool:
    """Whether an option is given at all: not its default (``None``, ``-1``
    or an empty sequence)."""
    return not (value is None or value == -1 or (isinstance(value, (tuple, list)) and not value))


def _resolve(program: Any, env: dict[str, str] | None) -> str:
    """*program* as a path ``posix_spawn`` can start, searched on the PATH
    the child will have; :class:`FileNotFoundError` when there is none."""
    name = os.fsdecode(os.fspath(program))
    if os.path.dirname(name):
        return name
    found = shutil.which(name, path=os.pathsep.join(os.get_exec_path(env)))
    if found is None:
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), name)
    return found


def _lift(stream: Any, opened: list[int]) -> Any:
    """*stream* as a descriptor above 2 when it is one of 0-2, else as given."""
    if stream is None or isinstance(stream, int) and stream < 0:
        return stream
    fd = stream if isinstance(stream, int) else stream.fileno()
    if fd > 2:
        return stream
    copy = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 3)
    opened.append(copy)
    return copy


def _prepare(
    args: Any, kwargs: dict[str, Any], *, captures_stdout: bool = False
) -> tuple[Any, dict[str, Any], list[int]]:
    """The call that starts the same child without ``fork()``: new arguments,
    new options, and the descriptors to close once the child has started.
    *captures_stdout* says the caller pipes the child's output itself
    (:func:`check_output`), so ``stderr=STDOUT`` joins that pipe."""
    if not _avoids_fork() or any(_set(kwargs.get(k)) for k in _FORK_ONLY_OPTIONS):
        return args, kwargs, []
    kw = dict(kwargs)
    env = kw.get("env")
    shell = kw.pop("shell", False)
    if shell:
        argv = ["/bin/sh", "-c", args] if isinstance(args, (str, bytes)) else ["/bin/sh", "-c", *args]
    else:
        argv = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
    program = _resolve(kw.pop("executable", None) or argv[0], env)

    cwd = kw.pop("cwd", None)
    new_session = bool(kw.pop("start_new_session", False))
    if cwd is not None:
        where = os.fsdecode(os.fspath(cwd))
        if not os.path.exists(where):
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), where)
        if not os.path.isdir(where):
            raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), where)
    if cwd is not None or new_session:
        restore = "1" if kw.get("restore_signals", True) else "0"
        argv = [
            sys.executable,
            "-I",
            "-S",
            "-c",
            _TRAMPOLINE,
            "1" if new_session else "0",
            restore,
            where if cwd is not None else "",
            program,
            *argv,
        ]
        program = sys.executable

    opened: list[int] = []
    for name in ("stdin", "stdout", "stderr"):
        if name in kw and kw[name] is not subprocess.STDOUT:
            kw[name] = _lift(kw[name], opened)
    if kw.get("stderr") is subprocess.STDOUT and kw.get("stdout") is None and not captures_stdout:
        kw["stderr"] = _lift(1, opened)
    kw["close_fds"] = False
    kw["executable"] = program
    return argv, kw, opened


def _close(opened: list[int]) -> None:
    for fd in opened:
        with contextlib.suppress(OSError):
            os.close(fd)


def _as_given(call: Callable[..., Any], args: Any, kwargs: dict[str, Any], *, captures_stdout: bool = False) -> Any:
    """Run *call* on the prepared launch; whatever it returns or raises names
    the command as the caller gave it."""
    prepared, kw, opened = _prepare(args, kwargs, captures_stdout=captures_stdout)
    try:
        result = call(prepared, **kw)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        exc.cmd = args
        raise
    finally:
        _close(opened)
    if isinstance(result, _CARRIES_ARGS):
        result.args = args
    return result


def run(args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
    """:func:`subprocess.run`, started without ``fork()`` on macOS."""
    return _as_given(subprocess.run, args, kwargs)


def Popen(args: Any, **kwargs: Any) -> subprocess.Popen:  # noqa: N802 — mirrors subprocess.Popen
    """:class:`subprocess.Popen`, started without ``fork()`` on macOS."""
    return _as_given(subprocess.Popen, args, kwargs)


def check_output(args: Any, **kwargs: Any) -> Any:
    """:func:`subprocess.check_output`, started without ``fork()`` on macOS."""
    return _as_given(subprocess.check_output, args, kwargs, captures_stdout=True)


def call(args: Any, **kwargs: Any) -> int:
    """:func:`subprocess.call`, started without ``fork()`` on macOS."""
    return _as_given(subprocess.call, args, kwargs)


#: Whether the fork-free browser has been put first in :mod:`webbrowser`.
_browser_registered = False


def _hand_to_open(url: str) -> bool:
    """Give a web address to macOS ``open``, started here; whether it took it.

    Only ``http``/``https``, and always as a URL (``-u``), so an address can
    never be read as an option, a file or an application; anything else is
    declined and :mod:`webbrowser` tries its next browser.
    """
    if urlsplit(url).scheme.lower() not in ("http", "https"):
        return False
    try:
        return run(["/usr/bin/open", "-u", url], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def open_url(url: str, **how: Any) -> bool:
    """Open *url* in the person's browser (:func:`webbrowser.open`, given
    *how*); whether it was handed over.

    On macOS, unless ``BROWSER`` names a browser, a browser that hands the
    address to ``open`` started here is put first in :mod:`webbrowser`'s
    order, because the standard library's own macOS browser forks.  Every
    other ``webbrowser.open`` in the process then uses it too.
    """
    import webbrowser

    global _browser_registered
    if _avoids_fork() and not os.environ.get("BROWSER") and not _browser_registered:

        class _Open(webbrowser.BaseBrowser):
            def open(self, url: str, new: int = 0, autoraise: bool = True) -> bool:
                return _hand_to_open(url)

        webbrowser.register("kiln-open", None, _Open("kiln-open"), preferred=True)
        _browser_registered = True
    return bool(webbrowser.open(url, **how))
