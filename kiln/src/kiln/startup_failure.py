"""Make a crash during MCP server startup legible instead of silent.

Everything ``kiln serve`` does before ``mcp.run()`` — loading config,
opening the database, registering printers, discovering plugins — runs
with no client attached.  Nothing has been written to stdout yet, so a
crash anywhere in that stretch closes the JSON-RPC pipe before a single
byte crosses it.  The MCP host has nothing to show but "server failed to
start".

Measured on this repo (2026-08-12) against a database that predates the
``geometric_signature_v2`` column, the crash fixed in ``8e14b88d``:

    exit code 1
    stdout    0 bytes          <- the client sees EOF, and says so
    stderr    a 30-line Python traceback ending in
              "sqlite3.OperationalError: no such column:
              geometric_signature_v2"
    on disk   nothing

Most MCP hosts discard or bury stderr, so in practice the user's whole
signal was: Kiln stopped existing.  ``kiln doctor`` — the one place a
stuck user is likely to look — reported ``✓ Database: writable``, because
its check opens the file with raw ``sqlite3`` and never asks
:class:`~kiln.persistence.KilnDB` to open it.  A confident all-clear on
the exact thing that was broken.

That particular schema bug is fixed.  The silence around it is what this
module is for, because the next startup-time exception inherits it.

Three doors read from here, one helper behind them:

* ``kiln serve`` calls :func:`record` on any startup exception, prints
  :func:`stderr_report` for whoever can see stderr, and then hands off to
  :func:`serve_safe_mode` so the client gets a server that comes up and
  can say what happened, instead of no server at all.
* ``kiln doctor`` / ``kiln quickstart`` call :func:`probe_database` (the
  real open, not a writability poke) and :func:`read` (last crash, if
  any).

Nothing in here may raise.  It runs on the failure path, where the
process has already proven it is having a bad day; a diagnostic that
throws would replace a legible failure with a worse one.  It also stays
deliberately free of Kiln's own machinery — no ``tool_results``
envelopes, no persistence layer at import time — because the thing that
just crashed may well be that machinery.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

#: Where the breadcrumb lands.  A fixed, boring name under ``~/.kiln`` so
#: it can be named in an error message, a doc, and a support reply
#: without anyone having to go looking for it.
_BREADCRUMB_NAME = "last-startup-error.log"

#: Set to opt out of safe mode and get the old behaviour back: the
#: process dies and the host reports a generic startup failure.  Here for
#: supervisors that treat "process is up" as "service is healthy" and
#: would rather crash-loop than serve a server that cannot print.
_DISABLE_SAFE_MODE_ENV = "KILN_DISABLE_SAFE_MODE"


def kiln_home() -> Path:
    """``~/.kiln`` (override with ``KILN_HOME``), created on demand."""
    d = Path(os.environ.get("KILN_HOME", "").strip() or (Path.home() / ".kiln"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def breadcrumb_path() -> Path:
    """Absolute path to the startup-failure breadcrumb."""
    return kiln_home() / _BREADCRUMB_NAME


# ---------------------------------------------------------------------------
# Diagnosis
# ---------------------------------------------------------------------------

#: Step one from every surface except ``kiln doctor`` itself, which would
#: otherwise advise a user to run the command they are already running.
#: Doctor filters it out with :meth:`Diagnosis.steps_elsewhere`.
DOCTOR_STEP = "Run `kiln doctor` — it re-checks this and prints what it finds."


@dataclass(frozen=True)
class Diagnosis:
    """One startup failure, in words a user can act on.

    ``kind`` is a stable slug for tests and for JSON consumers; the three
    prose fields are what a human actually reads.  ``what_to_do`` is
    ordered cheapest-and-safest first, so following it top to bottom
    never destroys anything before a harmless step has had its chance.
    """

    kind: str
    headline: str
    what_happened: str
    what_to_do: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "headline": self.headline,
            "what_happened": self.what_happened,
            "what_to_do": list(self.what_to_do),
        }

    def steps_elsewhere(self) -> list[str]:
        """The advice minus "run ``kiln doctor``", for ``kiln doctor``.

        Every other surface leads with it, because it is the cheapest
        next move.  Printed by doctor itself it is a loop, and a tool
        that answers a question by asking you to ask it again reads as
        broken even when the rest of the advice is good.
        """
        return [s for s in self.what_to_do if s != DOCTOR_STEP]


def explain(exc: BaseException) -> Diagnosis:
    """Translate a startup exception into plain English.

    Pattern-matched on the exception type and message.  The point is not
    to enumerate every possible failure — it is that the *fallback* is
    still useful, so an unrecognised crash degrades to "here is what
    broke and where the details are" rather than back to silence.
    """
    try:
        name = type(exc).__name__
        text = str(exc)
        low = text.lower()

        db_path = os.environ.get("KILN_DB_PATH") or str(kiln_home() / "kiln.db")

        # An older database meeting a newer schema.  The 8e14b88d crash,
        # and the shape any future migration bug will take.
        if "no such column" in low or "no such table" in low:
            return Diagnosis(
                kind="database_schema",
                headline="Kiln's database is missing something this version expects.",
                what_happened=(
                    f"Kiln could not open its database at {db_path}. It was "
                    f"created by a different version of Kiln and does not "
                    f"have everything this one needs ({text}). Your prints "
                    f"and history are still in the file — Kiln just could "
                    f"not finish preparing it."
                ),
                what_to_do=[
                    DOCTOR_STEP,
                    "Update Kiln — this is usually a migration that a newer "
                    "release already fixes: `pip install --upgrade kiln3d`.",
                    "Still stuck? Move the database aside and let Kiln build "
                    f"a fresh one: `mv {db_path} {db_path}.bak`. This starts "
                    "you over with no print history, so keep the .bak file "
                    "and mention it if you report the problem.",
                ],
            )

        # Corruption, or a file that was never a database.
        if "not a database" in low or "malformed" in low or "database disk image" in low:
            return Diagnosis(
                kind="database_corrupt",
                headline="Kiln's database file is damaged.",
                what_happened=(
                    f"The file at {db_path} is not readable as a SQLite "
                    f"database ({text}). That usually means it was truncated "
                    f"by a full disk, interrupted mid-write, or replaced by "
                    f"something else."
                ),
                what_to_do=[
                    DOCTOR_STEP,
                    "Check the disk has free space.",
                    f"Move the damaged file aside so Kiln can start fresh: "
                    f"`mv {db_path} {db_path}.bak`. Print history in the old "
                    "file is likely unrecoverable, but keep it anyway.",
                ],
            )

        # Locked, unwritable, or a directory Kiln cannot reach.
        if (
            isinstance(exc, PermissionError)
            or "unable to open database" in low
            or "readonly database" in low
            or "permission denied" in low
        ):
            return Diagnosis(
                kind="permissions",
                headline="Kiln cannot write to the files it needs.",
                what_happened=(
                    f"Kiln was denied access while starting up ({name}: "
                    f"{text}). Everything it stores lives under "
                    f"{kiln_home()}, so this is usually that folder's "
                    f"ownership or permissions, a full disk, or a database "
                    f"file left locked by another process."
                ),
                what_to_do=[
                    DOCTOR_STEP,
                    f"Check you own {kiln_home()} and can write to it.",
                    "Close any other Kiln servers that may be holding the "
                    "database open: `kiln trim-servers`.",
                    "Check the disk is not full.",
                ],
            )

        # A broken or half-finished install.
        if isinstance(exc, ImportError):
            return Diagnosis(
                kind="broken_install",
                headline="Part of Kiln's install is missing.",
                what_happened=(
                    f"Kiln could not import something it needs to start "
                    f"({name}: {text}). This normally means an interrupted "
                    f"upgrade, or two Kiln installs sharing one environment."
                ),
                what_to_do=[
                    DOCTOR_STEP,
                    "Reinstall Kiln: `pip install --force-reinstall kiln3d`.",
                    "If you use a virtual environment, make sure the one "
                    "your MCP client launches is the one you installed into.",
                ],
            )

        return Diagnosis(
            kind="unknown",
            headline="Kiln's server hit an error while starting up.",
            what_happened=(
                f"Startup stopped with {name}: {text}. Kiln does not have a "
                f"specific explanation for this one, so the full technical "
                f"details are worth passing on if you report it."
            ),
            what_to_do=[
                DOCTOR_STEP,
                "Update Kiln in case it is already fixed: "
                "`pip install --upgrade kiln3d`.",
                f"Report it with the file at {breadcrumb_path()} attached: "
                "https://github.com/codeofaxel/Kiln/issues",
            ],
        )
    except Exception:  # noqa: BLE001 — a diagnosis must never be the crash
        return Diagnosis(
            kind="unknown",
            headline="Kiln's server hit an error while starting up.",
            what_happened="Kiln could not describe the failure any further.",
            what_to_do=[DOCTOR_STEP],
        )


# ---------------------------------------------------------------------------
# The breadcrumb
# ---------------------------------------------------------------------------


def _version() -> str:
    try:
        import kiln

        return str(getattr(kiln, "__version__", "unknown"))
    except Exception:  # noqa: BLE001
        return "unknown"


def record(exc: BaseException, *, phase: str = "startup") -> Path | None:
    """Write the breadcrumb.  Returns its path, or ``None`` if even that failed.

    Plain English first, traceback last.  The ordering is the whole
    point: the user opening this file should reach the answer before
    they reach anything that looks like a stack trace, and the people
    who do want the trace lose nothing by scrolling.

    Overwrites rather than appends — the question a reader has is "why
    is Kiln broken *now*", and a growing log buries that under history.
    """
    diagnosis = explain(exc)
    try:
        import datetime

        when = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        steps = "\n".join(f"  {i}. {s}" for i, s in enumerate(diagnosis.what_to_do, 1))
        trace = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
        body = (
            f"Kiln could not start.\n"
            f"\n"
            f"{diagnosis.headline}\n"
            f"\n"
            f"WHAT HAPPENED\n"
            f"{diagnosis.what_happened}\n"
            f"\n"
            f"WHAT TO DO\n"
            f"{steps}\n"
            f"\n"
            f"WHEN            {when}\n"
            f"KILN VERSION    {_version()}\n"
            f"PYTHON          {sys.version.split()[0]}\n"
            f"FAILED DURING   {phase}\n"
            f"ERROR           {type(exc).__name__}: {exc}\n"
            f"DIAGNOSIS       {diagnosis.kind}\n"
            f"\n"
            f"TECHNICAL DETAIL (for a bug report — you do not need to read this)\n"
            f"{trace}"
        )
        path = breadcrumb_path()
        path.write_text(body, encoding="utf-8")
        return path
    except Exception as write_exc:  # noqa: BLE001
        logger.debug("could not write startup breadcrumb: %s", write_exc)
        return None


def read() -> dict | None:
    """Read the breadcrumb back, or ``None`` if there is not one.

    Returns the parsed header fields plus the raw text, so ``kiln
    doctor`` can summarise in one line and still point at the file.
    """
    try:
        path = breadcrumb_path()
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not read startup breadcrumb: %s", exc)
        return None

    fields: dict[str, str] = {}
    for label in ("WHEN", "KILN VERSION", "ERROR", "DIAGNOSIS", "FAILED DURING"):
        for line in text.splitlines():
            if line.startswith(label):
                fields[label.lower().replace(" ", "_")] = line[len(label):].strip()
                break

    headline = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and stripped != "Kiln could not start.":
            headline = stripped
            break

    return {
        "path": str(path),
        "headline": headline,
        "when": fields.get("when", ""),
        "error": fields.get("error", ""),
        "kind": fields.get("diagnosis", ""),
        "kiln_version": fields.get("kiln_version", ""),
        "phase": fields.get("failed_during", ""),
        "text": text,
    }


def clear() -> None:
    """Delete the breadcrumb.  Called once the server is actually serving.

    Without this, one bad launch makes ``kiln doctor`` cry wolf forever
    and users learn to ignore it — which costs more than the silence
    this module exists to fix.
    """
    try:
        breadcrumb_path().unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not clear startup breadcrumb: %s", exc)


# ---------------------------------------------------------------------------
# stderr
# ---------------------------------------------------------------------------


def stderr_report(diagnosis: Diagnosis, breadcrumb: Path | None) -> str:
    """The block printed to stderr, for whoever can see it."""
    lines = [
        "",
        "  ✗ Kiln could not start.",
        "",
        f"    {diagnosis.headline}",
        "",
        f"    {diagnosis.what_happened}",
        "",
        "    What to do:",
    ]
    lines += [f"      {i}. {s}" for i, s in enumerate(diagnosis.what_to_do, 1)]
    if breadcrumb is not None:
        lines += ["", f"    Full details: {breadcrumb}"]
    lines.append("")
    return "\n".join(lines)


def report_to_stderr(diagnosis: Diagnosis, breadcrumb: Path | None) -> None:
    """Print :func:`stderr_report`, never raising.

    Direct to stderr, not through the logger: a JSON log formatter or a
    silenced logger must not be able to swallow the one human-readable
    thing this process will ever emit.
    """
    with contextlib.suppress(Exception):
        print(stderr_report(diagnosis, breadcrumb), file=sys.stderr, flush=True)


def handle(exc: BaseException, *, phase: str = "startup") -> Diagnosis:
    """Record the failure and announce it.  The one call every door makes."""
    diagnosis = explain(exc)
    breadcrumb = record(exc, phase=phase)
    logger.error("Kiln startup failed (%s): %s", diagnosis.kind, exc, exc_info=True)
    report_to_stderr(diagnosis, breadcrumb)
    return diagnosis


# ---------------------------------------------------------------------------
# The database probe — what ``kiln doctor`` should have been doing
# ---------------------------------------------------------------------------


def probe_database() -> Diagnosis | None:
    """Open the database the way the server does.  ``None`` means it opened.

    ``kiln doctor``'s database check used to connect with raw ``sqlite3``,
    create a scratch table and drop it again.  That answers "is this file
    writable", which was never the question — the server dies in
    :meth:`KilnDB._ensure_schema`, several steps further in, and a
    database can be perfectly writable and still fail there.  On the
    2026-08-12 crash the check passed.

    Opening the real thing is also the honest answer for a second reason:
    ``KilnDB`` resolves ``KILN_DB_PATH`` itself, so this looks at the file
    the server will actually use rather than assuming ``~/.kiln/kiln.db``.

    Opening runs the migrations, which is a side effect worth naming: on
    a database that is merely out of date, running this check repairs it.
    That is a feature — the previous check wrote to the database too, and
    got nothing for it.
    """
    try:
        from kiln.persistence import KilnDB
    except Exception as exc:  # noqa: BLE001
        return explain(exc)

    db = None
    try:
        db = KilnDB()
        return None
    except Exception as exc:  # noqa: BLE001
        return explain(exc)
    finally:
        if db is not None:
            with contextlib.suppress(Exception):
                db.close()


# ---------------------------------------------------------------------------
# Safe mode
# ---------------------------------------------------------------------------


def safe_mode_enabled() -> bool:
    return os.environ.get(_DISABLE_SAFE_MODE_ENV, "").strip().lower() not in (
        "1",
        "true",
        "yes",
    )


def _payload(diagnosis: Diagnosis, breadcrumb: Path | None) -> dict:
    return {
        "status": "error",
        "kiln_running": False,
        "safe_mode": True,
        "headline": diagnosis.headline,
        "what_happened": diagnosis.what_happened,
        "what_to_do": list(diagnosis.what_to_do),
        "diagnosis": diagnosis.kind,
        "details_at": str(breadcrumb) if breadcrumb else None,
        "note": (
            "Kiln is in recovery mode: it could not finish starting, so no "
            "printing, slicing or design tools are available in this "
            "session. Fix the problem above and restart the server."
        ),
    }


def build_safe_mode_server(diagnosis: Diagnosis, breadcrumb: Path | None):
    """A minimal MCP server whose entire job is to explain the failure.

    Built from a fresh server object with none of Kiln's machinery
    attached, because the machinery is what just failed.  The three tools
    are the ones an agent actually reaches for — the server instructions
    have told every client since forever to open with ``get_started()``,
    so that name answering with the diagnosis is what puts the
    explanation in front of the user without them knowing to ask.
    """
    from kiln.mcp_compat import FastMCP

    payload = _payload(diagnosis, breadcrumb)
    steps = "\n".join(f"{i}. {s}" for i, s in enumerate(diagnosis.what_to_do, 1))
    instructions = (
        "KILN IS NOT RUNNING NORMALLY. The server could not finish "
        "starting, so it is serving in recovery mode: no printing, "
        "slicing, design or printer tools exist in this session.\n\n"
        f"{diagnosis.headline}\n\n{diagnosis.what_happened}\n\n"
        f"What to do:\n{steps}\n\n"
        "Tell the user this plainly. Do not attempt Kiln operations — "
        "they are not available until the problem is fixed and the "
        "server restarts."
    )

    server = FastMCP("kiln", instructions=instructions)

    @server.tool()
    def get_started() -> dict:
        """Kiln could not start — read this for what went wrong and how to fix it."""
        return dict(payload)

    @server.tool()
    def kiln_health() -> dict:
        """Kiln could not start — read this for what went wrong and how to fix it."""
        return dict(payload)

    @server.tool()
    def kiln_startup_diagnosis() -> dict:
        """Why the Kiln server failed to start, in plain language, with the fix."""
        return dict(payload)

    return server


def serve_safe_mode(diagnosis: Diagnosis, breadcrumb: Path | None) -> bool:
    """Serve the recovery server.  ``False`` if it could not be started.

    A user whose Kiln vanished has to already suspect Kiln, already know
    ``kiln doctor`` exists, and already be willing to leave their
    assistant and open a terminal.  Safe mode removes all three
    requirements: the client connects, the tool list is three tools long,
    and asking "what's wrong with Kiln?" returns the answer.

    A caller that gets ``False`` should exit non-zero, which is also what
    happens when the user has opted out via ``KILN_DISABLE_SAFE_MODE``.
    """
    if not safe_mode_enabled():
        return False
    try:
        server = build_safe_mode_server(diagnosis, breadcrumb)
    except Exception as exc:  # noqa: BLE001
        logger.debug("safe mode could not be built: %s", exc)
        return False
    with contextlib.suppress(Exception):
        print(
            "  → Kiln is starting in recovery mode so it can tell you this "
            "in your client.\n",
            file=sys.stderr,
            flush=True,
        )
    try:
        server.run()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("safe mode server exited with an error: %s", exc)
        return False
