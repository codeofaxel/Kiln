"""The one way Kiln launches OpenSCAD, so one place owns the crash retry.

Every OpenSCAD invocation in Kiln — STL compiles, PNG previews, syntax
validation, even the ``--version`` and ``--help`` probes — goes through
:func:`run_openscad`.  Nothing else in the package may call
``subprocess.run`` on an OpenSCAD binary; ``test_openscad_runner.py``
pins that, because a shared helper nobody calls is the same bug with
extra steps.

WHY THIS EXISTS
---------------
OpenSCAD can die of SIGSEGV during startup, before it has read a single
line of the SCAD it was handed.  Captured 2026-08-15 on macOS 26.5.2 with
OpenSCAD 2026.04.26 (arm64), launched by a Kiln process:

    Thread 1  EXC_BAD_ACCESS (SIGSEGV), KERN_INVALID_ADDRESS at 0x48
      localeconv_l + 80
      <- nlohmann::json serializer <- CoreAnalytics
      <- SecCoreAnalytics sendEventLazy <- Security framework
      <- SecStaticCodeCheckValidityWithErrors
      <- Qt 6.8 SandboxChecker::SandboxChecker()   [spawned thread]

    Thread 0  (main)
      _os_unfair_lock_lock_slow <- setlocale <- libintl_setlocale
      <- localization_init() <- openscad_main

Read the two stacks together and it is a startup race on the
process-global locale: Qt's sandbox-check thread reaches macOS analytics,
which reaches the locale, while OpenSCAD's main thread is still inside
``localization_init()`` mutating it.  The crashing thread's registers
point into ``__global_locale`` and the faulting read is a byte off a
pointer caught mid-swap.

That diagnosis is read off the stacks, not off the heap — nobody
single-stepped the swap.  It does hold up across samples: of the twelve
OpenSCAD crash reports on the affected machine between 2026-08-09 and
2026-08-15, ALL twelve caught the main thread inside
``localization_init()`` and all twelve faulted in ``localeconv_l``,
reached by three different Security-framework paths (the analytics
serializer above, ``snprintf`` under ``decodeTimeStampTokenWithPolicy``,
and ``snprintf`` under ``Security::MacOSError``).  Three unrelated
callers crashing in the same place is what a bad shared global looks
like; it is not a bug in any one of them.

What matters for Kiln is the shape, and the shape is not in doubt: the
crash lands before any SCAD is parsed, so it is a per-invocation lottery
independent of the model.  It is an upstream OpenSCAD/Qt/macOS problem,
not a Kiln bug.  But a user does not see "upstream race" — they see a
generation or render that failed for no stated reason, and succeeded
when they tried again.  So Kiln tries again for them.

Rate, so nobody has to guess later: roughly 1-2 a day on one active
machine.  Rare per launch, routine per week.

The precedent is :data:`kiln.slicer._ORCA_SIGSEGV_RETURNCODES`, which
handles the equivalent crash in OrcaSlicer, and this module keeps its
honesty about what a return code can and cannot distinguish — including
its salvage rule for a process that died *after* writing good output.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
No fixed child locale.  Pinning ``LC_ALL=C``/``LANG=C`` for the child
should make ``localization_init()`` trivial and so narrow the window the
sandbox-check thread has to race it.  Tidy story, and it is NOT shipped,
because the attempt to test it failed rather than the idea.

Tested 2026-08-15 on the affected build, 400 launches per arm: zero
crashes with the inherited environment, zero with ``LC_ALL=C``.  Zero on
both sides cannot separate them.  Worse, that probe almost certainly had
no chance of reproducing the bug at all: every crash goes through
``SecStaticCodeCheckValidityWithErrors``, macOS caches code-signature
validation, and 800 back-to-back launches of one binary are all
cache-warm after the first.  So 0/400 bounds the warm, repeated-launch
rate — not the rate a user meets, which the crash reports put at 1-2 a
day.  Anyone retrying this needs to invalidate that cache between
launches, or the arms will keep agreeing at zero.

A first run also appeared to show ``LC_ALL=C`` halving startup
(94.5ms -> 43.5ms).  That was arm order, not the locale: reversed and
interleaved, both arms sit at 43-44ms, and the gap was the first arm
paying cold-cache costs on the second's behalf.

So the locale idea is untested, not disproved, and it ships as neither.
A guess shipped as a fix is worse than no fix — it stops the next person
from looking for the real cause.  The retry works whatever that cause
turns out to be, so the retry is what ships.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from collections.abc import Sequence

logger = logging.getLogger(__name__)

__all__ = ["OPENSCAD_CRASH_RETURNCODES", "run_openscad"]


#: Signals treated as "the process crashed on its way up, try again".
#:
#: Only the memory-fault signals.  The exclusions are the point:
#:
#: - ``SIGABRT`` is how a C++ assertion or an uncaught exception exits.
#:   OpenSCAD can abort on genuinely bad geometry, and that is a real
#:   answer about a real model — retrying it burns time to print the
#:   same message twice.
#: - ``SIGKILL`` is the OOM killer or a human.  A retry of an
#:   out-of-memory kill is a second trip to the same wall, on a machine
#:   that just told us it is out of room.
#: - ``SIGTERM``/``SIGINT`` are somebody asking for this to stop.
#:   Restarting it is the opposite of what was asked.
_CRASH_SIGNALS = frozenset({signal.SIGSEGV, signal.SIGBUS})

#: Return codes that mean one of :data:`_CRASH_SIGNALS` killed the child.
#:
#: :func:`subprocess.run` reports a signal death as a negative return
#: code.  The ``128 + N`` spellings cannot arise from this module — it
#: never launches through a shell — and are carried for the same reason
#: :mod:`kiln.slicer` carries them: cheap, and correct if a caller ever
#: hands a result from elsewhere to :func:`crashed_on_startup`.
OPENSCAD_CRASH_RETURNCODES = frozenset(
    {-int(sig) for sig in _CRASH_SIGNALS} | {128 + int(sig) for sig in _CRASH_SIGNALS}
)

#: How long a run may take and still be blamed on the startup race.
#:
#: The captured crash happens in ``localization_init()`` — before argument
#: parsing, before the file is opened, milliseconds into the process.  A
#: segfault forty seconds into a CGAL boolean is a different animal, and
#: retrying it means waiting out that boolean two more times to be told
#: the same thing.  Ten seconds is far past any plausible startup on a
#: loaded machine and far short of real geometry work.
_STARTUP_CRASH_WINDOW_S = 10.0

#: Total attempts, so two retries after the first crash.
#:
#: A startup-lottery crash is independent per launch, so a third ticket
#: costs milliseconds and buys real coverage.  Beyond that the odds say
#: it is not the lottery, and the honest move is to report the crash
#: rather than keep pulling the handle.
_DEFAULT_ATTEMPTS = 3


def _signal_name(returncode: int) -> str:
    """Human-readable name for a crash return code (``-11`` -> ``SIGSEGV``)."""
    number = -returncode if returncode < 0 else returncode - 128
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"signal {number}"


def _wrote_output(output_path: str | None) -> bool:
    """Whether *output_path* names a file with bytes in it.

    Deliberately only "non-empty", not "valid": this decides whether a
    retry is worth trying, and the caller is the one that knows what a
    good STL or PNG looks like.  Every call site already validates its
    own output — see the ``st_size > 84`` binary-STL check in
    :mod:`kiln.model_visualizer` — and a crash that still wrote
    something belongs in front of that check, not in place of it.
    """
    if not output_path:
        return False
    try:
        return os.path.getsize(output_path) > 0
    except OSError:
        return False


def crashed_on_startup(result: subprocess.CompletedProcess) -> bool:
    """Whether *result* looks like the upstream OpenSCAD startup crash.

    Exposed so error-message code can say "this was a known upstream
    crash, not your model" without re-deriving the return codes.
    """
    return result.returncode in OPENSCAD_CRASH_RETURNCODES


def run_openscad(
    cmd: Sequence[str],
    *,
    timeout: float | None = None,
    cwd: str | None = None,
    text: bool = True,
    output_path: str | None = None,
    attempts: int = _DEFAULT_ATTEMPTS,
) -> subprocess.CompletedProcess:
    """Run an OpenSCAD command, retrying the upstream startup crash.

    A drop-in for the ``subprocess.run(cmd, capture_output=True, ...)``
    every call site used to make.  Output is always captured, because
    every OpenSCAD site in Kiln wants stderr.

    A rerun happens only when all of the following hold, and the whole
    point is what each one rules out:

    - the child died of :data:`_CRASH_SIGNALS` — so a non-zero exit with
      a real OpenSCAD diagnostic on stderr is returned untouched.  A
      syntax error in a user's SCAD is an answer, and retrying an answer
      just hides it behind a delay;
    - it wrote nothing to *output_path* — so a crash after a complete
      write is handed back for the caller to salvage, the same call the
      Orca path in :mod:`kiln.slicer` makes;
    - it died inside :data:`_STARTUP_CRASH_WINDOW_S` — so a crash during
      real geometry work is reported once instead of three times;
    - attempts remain.

    Timeouts and :exc:`OSError` (a missing or unrunnable binary) are not
    caught.  They propagate to the caller, which already handles both
    and would only be confused by three of them.

    :param cmd: Full argv, starting with the OpenSCAD binary path.
    :param timeout: Per-attempt timeout in seconds.  A retry gets a
        fresh one: a crash inside the startup window consumed none of it.
    :param cwd: Working directory for the child.
    :param text: Decode stdout/stderr as text.  ``False`` matches the
        sites that only test the return code and want bytes.
    :param output_path: The file this command is supposed to produce, if
        it produces one.  Without it a crash cannot be told from a crash
        that finished the job first, and the retry becomes willing to
        throw away good output.
    :param attempts: Total attempts including the first.  ``1`` disables
        retrying entirely.
    :returns: The :class:`subprocess.CompletedProcess` of the last
        attempt — a crash that outlives every retry is still returned,
        not raised, so existing error handling at each site keeps working.
    """
    argv = list(cmd)
    result: subprocess.CompletedProcess | None = None

    for attempt in range(1, max(1, attempts) + 1):
        started = time.monotonic()
        result = subprocess.run(  # noqa: S603 — argv is built by Kiln, never shell
            argv,
            capture_output=True,
            text=text,
            timeout=timeout,
            cwd=cwd,
        )
        elapsed = time.monotonic() - started

        if not crashed_on_startup(result):
            return result

        if _wrote_output(output_path):
            logger.warning(
                "OpenSCAD died on %s after writing %s — keeping the output and "
                "letting the caller validate it.",
                _signal_name(result.returncode),
                os.path.basename(output_path or ""),
            )
            return result

        if elapsed > _STARTUP_CRASH_WINDOW_S:
            logger.error(
                "OpenSCAD died on %s after %.1fs of work. That is too late to be "
                "the known startup crash, so it is not being retried — this one "
                "looks like the model.",
                _signal_name(result.returncode),
                elapsed,
            )
            return result

        if attempt >= max(1, attempts):
            logger.error(
                "OpenSCAD died on %s during startup on all %d attempts. This is a "
                "known upstream crash in OpenSCAD/Qt on macOS, not a problem with "
                "the model — it happens before the file is read. Try again, or see "
                "https://github.com/openscad/openscad/issues if it is persistent.",
                _signal_name(result.returncode),
                attempt,
            )
            return result

        logger.warning(
            "OpenSCAD died on %s during startup after %.2fs, before reading any "
            "input, and wrote nothing — retrying (attempt %d of %d). This is a "
            "known upstream crash, not a problem with the model.",
            _signal_name(result.returncode),
            elapsed,
            attempt + 1,
            max(1, attempts),
        )

    # Unreachable: the loop runs at least once and every branch returns.
    assert result is not None
    return result
