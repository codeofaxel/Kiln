"""The one way Kiln launches OpenSCAD, so one place owns the crash retry.

Every OpenSCAD invocation in Kiln — STL compiles, PNG previews, syntax
validation, even the ``--version`` and ``--help`` probes — goes through
:func:`run_openscad`.  Nothing else in the package may call
``subprocess.run`` on an OpenSCAD binary; ``test_openscad_runner.py``
pins that, because a shared helper nobody calls is the same bug with
extra steps.

It is also where a source written as named modules composed by ``main()``
compiles to that ``main()`` (:func:`kiln.scad_main.runnable_inputs`), so every
door that hands OpenSCAD a file gets the same answer.

It is where OpenSCAD finds the BOSL2 and MCAD libraries Kiln ships: every
launch runs with them first on ``OPENSCADPATH`` (:func:`openscad_env`).

And it is where a compile that left part of the model out stops counting as
a success.  OpenSCAD warns when it cannot find an included library, a file it
imports, or a module, then leaves that part out, exits 0 and writes the rest.
:func:`run_openscad` reports such a run as failed, names what was missing,
and deletes the partial file (:func:`left_out_parts`).

The same holds for a setting OpenSCAD ignored.  A module called with an
argument it has no parameter for gets a warning, keeps its default, and the
compile exits 0 with a part of the wrong size.  :func:`ignored_settings` reads
those warnings from the caller's own source (a bundled library's internals
are not the caller's to fix), and :func:`run_openscad` refuses the run,
naming each setting and its line.

And it is where a mesh the fast engine left open is rebuilt with the exact
one.  When a compile not already on CGAL writes an STL that is not closed, the
same command runs once more with ``--backend=cgal``, within
:data:`EXACT_ENGINE_TIMEOUT_S`, and its file is kept only if it is closed;
otherwise the first file stays.  An
OpenSCAD too old to know the option refuses it, and the first file stays.

The crash retry: when OpenSCAD dies of SIGSEGV before writing any output,
:func:`run_openscad` retries that crash and only that crash, on the rules its
own docstring lists, with the salvage rule
:data:`kiln.slicer._ORCA_SIGSEGV_RETURNCODES` follows for a process that died
*after* writing good output.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import signal
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

from kiln.scad_main import runnable_inputs

logger = logging.getLogger(__name__)

__all__ = [
    "OPENSCAD_CRASH_RETURNCODES",
    "bundled_library_path",
    "ignored_settings",
    "left_out_parts",
    "openscad_env",
    "present_signals",
    "run_openscad",
]


def present_signals(*names: str) -> frozenset[int]:
    """The numbers of the named signals this platform defines.

    CPython's :mod:`signal` module on Windows has no ``SIGBUS`` and no
    ``SIGTRAP``; reading either as an attribute raises at import time and
    takes every ``kiln`` entrypoint down with it (issue #146).  A crash
    set is built from whatever the platform offers and degrades, on
    Windows, to ``SIGSEGV`` alone — which is right: the startup race the
    sets exist for is a macOS libc bug, and a platform that cannot spell
    the signal cannot produce the crash.
    """
    return frozenset(
        int(sig) for sig in (getattr(signal, name, None) for name in names) if sig is not None
    )


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
_CRASH_SIGNALS = present_signals("SIGSEGV", "SIGBUS")

#: Return codes that mean one of :data:`_CRASH_SIGNALS` killed the child.
#:
#: :func:`subprocess.run` reports a signal death as a negative return
#: code.  The ``128 + N`` spellings cannot arise from this module — it
#: never launches through a shell — and are carried for the same reason
#: :mod:`kiln.slicer` carries them: cheap, and correct if a caller ever
#: hands a result from elsewhere to :func:`crashed_on_startup`.
OPENSCAD_CRASH_RETURNCODES = frozenset(
    {-sig for sig in _CRASH_SIGNALS} | {128 + sig for sig in _CRASH_SIGNALS}
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


def bundled_library_path() -> str:
    """The folder of OpenSCAD libraries Kiln ships (BOSL2, MCAD), in the wheel."""
    return str(Path(__file__).resolve().parent / "data" / "scad_libraries")


def openscad_env() -> dict[str, str]:
    """The environment every OpenSCAD launch gets.

    This process's environment, with Kiln's bundled libraries first on
    ``OPENSCADPATH``: first, so ``include <BOSL2/...>`` reaches the copy Kiln
    ships even where another one is installed.  Folders already on the path
    stay, after it.
    """
    env = dict(os.environ)
    library = bundled_library_path()
    if os.path.isdir(library):
        rest = [p for p in env.get("OPENSCADPATH", "").split(os.pathsep) if p and p != library]
        env["OPENSCADPATH"] = os.pathsep.join([library, *rest])
    return env


#: OpenSCAD's words for a part of the source it could not build, a pattern
#: that reads the name out of the warning, and what that name is.  After every
#: one of these OpenSCAD leaves that part out, exits 0 and writes the rest, so
#: a caller reading the exit code keeps a partial part as the whole one.  An
#: unknown FUNCTION is not here: it returns undef, which OpenSCAD reports again
#: where the value is used, and it leaves no part out by itself.  A name ends
#: at a quote followed by punctuation, a space or the end, because the
#: warnings themselves contain apostrophes ("Can't", "couldn't").
_LEFT_OUT_WARNINGS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("Can't find include file", re.compile(r"include file '(.+?)'(?=[.,\s]|$)"), "library"),
    ("Can't open library", re.compile(r"library '(.+?)'(?=[.,\s]|$)"), "library"),  # use <...>
    ("Can't open import file", re.compile(r"import file '(.+?)'(?=[.,\s]|$)"), "file"),
    ("couldn't be opened", re.compile(r"file '(.+)' couldn't be opened"), "file"),  # surface()
    ("Ignoring unknown module", re.compile(r"module '(.+?)'(?=[.,\s]|$)"), "module"),
)
#: How many missing names the failure message lists before "and N more".
_LEFT_OUT_NAMES_SHOWN = 6


def left_out_parts(stderr: str | bytes | None) -> list[tuple[str, str]]:
    """``(kind, name)`` for each part OpenSCAD said it left out, once each.

    Only OpenSCAD's own ``WARNING:`` lines count, so a source that echoes the
    same words is not misread.  A file is named by its base name: the folder
    is a temporary one on Kiln's side, and the name is what the source wrote.
    A warning whose name cannot be read still counts, named by its own text.
    """
    if not stderr:
        return []
    text = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr
    found: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("WARNING:"):
            continue
        for words, pattern, kind in _LEFT_OUT_WARNINGS:
            if words not in line:
                continue
            match = pattern.search(line)
            if match:
                name = os.path.basename(match.group(1)) if kind == "file" else match.group(1)
            else:
                name = line.removeprefix("WARNING:").strip()
            if (kind, name) not in found:
                found.append((kind, name))
            break
    return found


#: How each kind of missing part is named in the failure message.
_LEFT_OUT_LABELS = {"library": "library file", "file": "file", "module": "module"}


def _left_out_message(parts: list[tuple[str, str]]) -> str:
    """The sentence a failed caller shows: what was missing, and the fix."""
    shown = ", ".join(
        f"{_LEFT_OUT_LABELS[kind]} '{name}'" for kind, name in parts[:_LEFT_OUT_NAMES_SHOWN]
    )
    if len(parts) > _LEFT_OUT_NAMES_SHOWN:
        shown += f" and {len(parts) - _LEFT_OUT_NAMES_SHOWN} more"
    kinds = {kind for kind, _ in parts}
    fixes = []
    if "library" in kinds:
        # A missing library takes its modules with it: those are named too,
        # and defining them by hand is the wrong fix.
        fixes.append(
            "check the library file's name (Kiln ships BOSL2 and MCAD)"
            + ("; the missing modules most likely come from it" if "module" in kinds else "")
        )
    elif "module" in kinds:
        fixes.append("define the module or fix its name")
    if "file" in kinds:
        fixes.append("put the file where the source points")
    return (
        f"OpenSCAD left part of this model out (missing {shown}) and still reported "
        f"success, so Kiln deleted the partial file and counts the compile as failed. "
        f"To fix: {'; '.join(fixes)}."
    )


#: OpenSCAD's warning for an argument a module has no parameter for, across
#: versions (``variable "x"`` from 2021 on, a bare name before), with where it
#: was written when OpenSCAD says.
_IGNORED_SETTING = re.compile(
    r'variable "?([A-Za-z_$][\w$]*)"? not specified as parameter'
    r"(?: in file (.+?), line (\d+))?"
)
#: The bundled libraries: a warning raised inside one is not the caller's.
_LIBRARY_FOLDERS = ("BOSL2", "MCAD")


def ignored_settings(stderr: str | bytes | None) -> list[tuple[str, int | None]]:
    """``(setting, line)`` for each argument OpenSCAD ignored in the caller's
    own source, once each, in the order OpenSCAD reported them.

    Only OpenSCAD's own ``WARNING:`` lines count.  A warning raised inside a
    bundled library is left out: the caller cannot fix the library's call.
    """
    if not stderr:
        return []
    text = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr
    found: list[tuple[str, int | None]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("WARNING:"):
            continue
        match = _IGNORED_SETTING.search(line)
        if not match:
            continue
        where = match.group(2) or ""
        if any(part in _LIBRARY_FOLDERS for part in re.split(r"[\\/]", where)):
            continue
        entry = (match.group(1), int(match.group(3)) if match.group(3) else None)
        if entry not in found:
            found.append(entry)
    return found


def _ignored_message(settings: list[tuple[str, int | None]]) -> str:
    """The sentence a refused caller shows for settings OpenSCAD ignored."""
    shown = ", ".join(
        f"'{name}'" + (f" (line {line})" if line else "")
        for name, line in settings[:_LEFT_OUT_NAMES_SHOWN]
    )
    if len(settings) > _LEFT_OUT_NAMES_SHOWN:
        shown += f" and {len(settings) - _LEFT_OUT_NAMES_SHOWN} more"
    return (
        f"OpenSCAD ignored {shown}: the module it was given to has no setting by that name, "
        f"so the part would have come out at its default size, and Kiln counts the compile "
        f"as failed. To fix: use the module's own name for that setting."
    )


def _resolved_output(output_path: str | None, cwd: str | None) -> str | None:
    """*output_path* as the child saw it: relative paths are under *cwd*."""
    if not output_path:
        return None
    if cwd and not os.path.isabs(output_path):
        return os.path.join(cwd, output_path)
    return output_path


#: One of OpenSCAD's own messages: its kind in capitals, then a colon
#: (``WARNING:``, ``ECHO:``, ``TRACE:``, ``DEPRECATED:``, ``FONT-WARNING:``).
_OPENSCAD_MESSAGE = re.compile(r"^[A-Z][A-Z-]*:")


def _messages_only(stderr: str) -> str:
    """*stderr* without the report of a successful run (cache sizes, render
    time, the finished object's facet count), keeping OpenSCAD's messages.

    A refused run is a failure, and that report describes a part Kiln has just
    deleted; read as a failure's output, each line would be one more "error".
    """
    return "".join(
        line for line in stderr.splitlines(keepends=True) if _OPENSCAD_MESSAGE.match(line.lstrip())
    )


def _refused_for_left_out_parts(
    result: subprocess.CompletedProcess,
    parts: list[tuple[str, str]],
    *,
    cwd: str | None,
    output_path: str | None,
    ignored: list[tuple[str, int | None]] | None = None,
) -> subprocess.CompletedProcess:
    """*result* as a failed run: exit 1, the message first on stderr, no file.

    After the message come OpenSCAD's own messages (what it left out and
    where, the source's echoes), not its report of a run that went well.  The
    partial file is deleted so no caller that looks for the file before the
    exit code can pick it up.  ``left_out_parts`` on the returned object
    carries the names for a caller that wants them as data.
    """
    path = _resolved_output(output_path, cwd)
    if path:
        with contextlib.suppress(OSError):
            os.remove(path)
    said_why = [_left_out_message(parts)] if parts else []
    if ignored:
        said_why.append(_ignored_message(ignored))
    message = " ".join(said_why) + "\n"
    stderr = result.stderr
    if isinstance(stderr, bytes):
        said = _messages_only(stderr.decode("utf-8", "replace"))
        stderr = (message + said).encode("utf-8")
    else:
        stderr = message + _messages_only(stderr or "")
    refused = subprocess.CompletedProcess(result.args, 1, result.stdout, stderr)
    refused.left_out_parts = tuple(parts)  # type: ignore[attr-defined]
    refused.ignored_settings = tuple(ignored or ())  # type: ignore[attr-defined]
    logger.warning("%s", message.strip())
    return refused


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

    A drop-in for ``subprocess.run(cmd, capture_output=True, ...)`` at
    every OpenSCAD call site.  Output is always captured, because every
    OpenSCAD site in Kiln wants stderr.

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
        A run that exited 0 after leaving part of the model out, or after
        ignoring a setting in the caller's source, comes back with exit
        code 1, the reason first on stderr, ``left_out_parts`` and
        ``ignored_settings`` set, and *output_path* deleted.
    """
    with runnable_inputs(cmd, cwd=cwd) as argv:
        result = _run_with_retry(argv, timeout=timeout, cwd=cwd, text=text,
                                 output_path=output_path, attempts=attempts)
        if result.returncode == 0:
            parts = left_out_parts(result.stderr)
            ignored = ignored_settings(result.stderr)
            if parts or ignored:
                return _refused_for_left_out_parts(result, parts, cwd=cwd,
                                                   output_path=output_path, ignored=ignored)
        if result.returncode == 0 and _EXACT_ENGINE_FLAG not in argv:
            result = _closed_by_the_exact_engine(
                argv, result, timeout=timeout, cwd=cwd, text=text,
                output_path=output_path, attempts=attempts,
            )
        return result


#: The flag that picks OpenSCAD's exact CGAL backend.  OpenSCAD 2024+ runs
#: its fast Manifold backend unless told otherwise.
_EXACT_ENGINE_FLAG = "--backend=cgal"
#: The longest the exact engine may take to close what the fast one left
#: open.  CGAL can be far slower, so past this the fast engine's file stands
#: and the delay a person waits stays bounded.
EXACT_ENGINE_TIMEOUT_S = 60.0
#: Files above this are not loaded to check; the cost is memory, not time.
_CLOSURE_CHECK_MAX_BYTES = 256 * 1024 * 1024
#: A fast-engine file past this many triangles is not rebuilt with the exact
#: engine, because of memory: the rebuild costs about 15 MB per thousand
#: triangles and the hosted server has 4 GB for every caller at once.
#: Measured on relief carves on a laptop (97,606 triangles: 23.5 s, 1.5 GB;
#: 191,478: 50.1 s, 2.8 GB); a 280,000-triangle coaster ran to the 60 s
#: limit at 3.7 GB, every time, and its fast file stood anyway.  The parts
#: the rebuild exists for (a clipped bookmark's open seams, 196 triangles)
#: are far below this.  The cap holds on a local install too, so an open
#: part of 100,000 to about 240,000 triangles that the rebuild could have
#: closed in time keeps its fast file there as well.
_EXACT_ENGINE_MAX_TRIANGLES = 100_000
#: How far the exact engine's part may differ from the fast engine's and still
#: be the same part: its volume as a share of the fast one's (judged only when
#: the open file has a volume to judge; a file missing a whole face has none),
#: and its outer size as a share of the fast one's diagonal.  A closing seam
#: moves these by far less; a dropped feature moves them by far more
#: (OpenSCAD 2026.08.09's exact engine has returned a photo-relief coaster
#: without its relief, at 12% more volume, and without this check the closed
#: result would be taken).
_SAME_PART_SHARE = 0.01


def release_checked_mesh() -> None:
    """Free the trimesh mesh a check just loaded, now rather than whenever.

    A trimesh mesh holds reference cycles, so it outlives the check that
    loaded it until Python's cycle collector happens to run, and whatever
    runs next peaks on top of it: after a hollow check, a relief carve's
    face record measured 360 MB still held and peaked 180 MB higher than it
    does alone.  Collecting here makes each step's peak its own.

    Call it only when a mesh was built.  A full collection walks every
    tracked object in the process and holds every thread while it does: in
    the hosted API (about two million objects) that was 0.39 s a call,
    against 0.4 ms for a check the triangle arrays answer on their own.
    """
    import gc

    gc.collect()


def _is_open_mesh(path: str) -> bool:
    """True when *path* is an STL whose surface has open or over-shared edges.

    Anything it cannot judge (no file, not an STL, too big, trimesh absent or
    unable to read it) counts as not open, so the compile is left alone.
    """
    if not path.lower().endswith(".stl") or not os.path.isfile(path):
        return False
    if os.path.getsize(path) > _CLOSURE_CHECK_MAX_BYTES:
        return False
    mesh = None
    try:
        from kiln.mesh_closure import is_watertight
        from kiln.mesh_frame import load_mesh

        closed = is_watertight(path)
        if closed is not None:
            return not closed
        mesh = load_mesh(path, force="mesh")
        return len(mesh.faces) > 0 and not mesh.is_watertight
    except Exception:  # noqa: BLE001 — an unreadable file is the caller's to report
        logger.debug("closure check could not read %s", path, exc_info=True)
        return False
    finally:
        if mesh is not None:
            mesh = None  # the last reference, dropped so the collection can free it
            release_checked_mesh()


def _same_part(fast_path: str, exact_path: str) -> bool:
    """True when the exact engine's file is the fast engine's part, closed.

    Anything it cannot judge counts as the same part, as before this check.
    """
    try:
        import numpy as np

        from kiln.mesh_frame import load_mesh

        # The fast file is set aside as "<name>.stl.fast": name the type.
        fast = load_mesh(fast_path, file_type="stl", force="mesh")
        exact = load_mesh(exact_path, file_type="stl", force="mesh")
        if not len(fast.faces) or not len(exact.faces):
            return len(fast.faces) == len(exact.faces)
        diag = float(np.linalg.norm(fast.extents)) or 1.0
        if float(np.abs(fast.bounds - exact.bounds).max()) > _SAME_PART_SHARE * diag:
            return False
        fast_volume = abs(float(fast.volume))
        if not np.isfinite(fast_volume) or fast_volume <= _SAME_PART_SHARE * float(np.prod(fast.extents)):
            return True  # no volume to compare against; the outer size agreed
        return abs(abs(float(exact.volume)) - fast_volume) <= _SAME_PART_SHARE * fast_volume
    except Exception:  # noqa: BLE001 — an unreadable file leaves the old behaviour
        logger.debug("same-part check could not read %s / %s", fast_path, exact_path, exc_info=True)
        return True


def _closed_by_the_exact_engine(
    argv: list[str],
    fast: subprocess.CompletedProcess,
    *,
    timeout: float | None,
    cwd: str | None,
    text: bool,
    output_path: str | None,
    attempts: int,
) -> subprocess.CompletedProcess:
    """The exact engine's file when the fast one wrote an open mesh and it
    closes it into the same part; the fast file otherwise."""
    path = _resolved_output(output_path, cwd)
    if not path:
        return fast
    if path.lower().endswith(".stl"):
        from kiln.generation.validation import stl_facet_count

        count = stl_facet_count(path)
        if count is not None and count > _EXACT_ENGINE_MAX_TRIANGLES:
            # Read from the header before the closure check loads anything:
            # the fast file stands whether or not it is closed.
            logger.info("%s has %d triangles, past what the exact engine can rebuild in "
                        "its time; keeping the fast engine's file.", os.path.basename(path), count)
            return fast
    if not _is_open_mesh(path):
        return fast

    kept = f"{path}.fast"
    os.replace(path, kept)
    budget = EXACT_ENGINE_TIMEOUT_S if timeout is None else min(timeout, EXACT_ENGINE_TIMEOUT_S)
    exact_argv = [argv[0], _EXACT_ENGINE_FLAG,
                  *(arg for arg in argv[1:] if not arg.startswith("--backend"))]
    try:
        exact = _run_with_retry(exact_argv, timeout=budget, cwd=cwd, text=text,
                                output_path=output_path, attempts=attempts)
    except subprocess.TimeoutExpired:
        exact = None
    closed = exact is not None and exact.returncode == 0 and os.path.isfile(path) and not _is_open_mesh(path)
    if closed and not _same_part(kept, path):
        logger.warning("OpenSCAD's exact engine closed %s but changed the part (it lost or added "
                       "geometry the fast engine made); keeping the fast engine's file.",
                       os.path.basename(path))
        os.replace(kept, path)
        return fast
    if closed:
        os.remove(kept)
        logger.info("OpenSCAD's fast engine left %s open; the exact engine closed it.",
                    os.path.basename(path))
        return exact
    os.replace(kept, path)
    logger.warning("OpenSCAD's fast engine left %s open and the exact engine did not close "
                   "it within %.0fs; keeping the fast engine's file.", os.path.basename(path), budget)
    return fast


def _run_with_retry(
    argv: list[str],
    *,
    timeout: float | None,
    cwd: str | None,
    text: bool,
    output_path: str | None,
    attempts: int,
) -> subprocess.CompletedProcess:
    """The launch loop behind :func:`run_openscad`."""
    result: subprocess.CompletedProcess | None = None

    for attempt in range(1, max(1, attempts) + 1):
        started = time.monotonic()
        result = subprocess.run(  # noqa: S603 — argv is built by Kiln, never shell
            argv,
            capture_output=True,
            text=text,
            timeout=timeout,
            cwd=cwd,
            env=openscad_env(),
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
