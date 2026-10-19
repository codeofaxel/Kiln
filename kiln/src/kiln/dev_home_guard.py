"""A test or a sandboxed run never writes the developer's real ``~/.kiln``.

Kiln keeps a person's whole world under ``~/.kiln``: printers, print records,
calibration, credentials, the counters the heartbeat reports.  Both test
suites move ``HOME`` to a scratch folder before anything imports Kiln (their
``conftest.py``), and that holds for every test the suite collects.  It does
not hold for anything that runs Kiln's code WITHOUT the suite's setup: a test
file run some other way, a one-off script or probe run by hand, a check that
drives real tools.  On a developer's machine those wrote thousands of fake
prints, jobs and recoveries into the real records over five months (found and
cleaned 2026-10-18), and Kiln learns from those records.

So the rule is made at the one door every one of those paths passes through,
the import of :mod:`kiln` itself (and of :mod:`kiln_pro`, which imports it):
before any store can resolve a path, a process that is a TEST of a source
checkout, or one that ASKED to be a sandbox (``KILN_SANDBOX=1``), has its
``HOME`` moved to a fresh scratch folder whenever it still points at the
account's real home.  A store added next year is covered without knowing this
file exists.

Who it never touches: an installed Kiln (a ``pip install``) running anyone's
tests, because moving a stranger's ``HOME`` mid-run is not Kiln's call; a run
already pointed somewhere else (the suites, the probes); and a run that sets
``KILN_ALLOW_REAL_HOME=1``, the deliberate escape for testing against the real
records on purpose.  Everything else about Kiln's normal use is untouched: a
server, the command line and an agent's MCP session are not tests.

What it cannot see: a tool called on a Kiln that is already running for real
(the person's own MCP server) with made-up printers or files.  That is a real
call to a real install; the rule for it is the agents' (never exercise tools
with fake data through the person's live Kiln; use ``KILN_SANDBOX=1``).
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

#: Set to ``1`` to run this process in a throwaway home, test or not.
SANDBOX_ENV = "KILN_SANDBOX"
#: Set to ``1`` to let a test or a sandbox run use the real home on purpose.
ALLOW_REAL_HOME_ENV = "KILN_ALLOW_REAL_HOME"
#: Marks a process this module moved, and where it moved it to.
MOVED_ENV = "KILN_SANDBOX_HOME"

_TRUE = frozenset({"1", "true", "yes", "on"})


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE


def account_home() -> str:
    """The home the operating system gives this account, whatever ``HOME``
    says now: the place a person's real ``~/.kiln`` lives."""
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_dir
    except Exception:  # noqa: BLE001 — no pwd (Windows) or no entry
        return os.environ.get("USERPROFILE") or str(Path("~").expanduser())


def _inside(path: str, root: str) -> bool:
    try:
        real, base = os.path.realpath(path), os.path.realpath(root)
    except Exception:  # noqa: BLE001
        return False
    return real == base or real.startswith(base + os.sep)


def is_source_checkout(package_file: str | None = None) -> bool:
    """Whether Kiln runs from a source checkout (a developer's tree), not an
    installed package: the package sits at ``kiln/src/kiln`` beside the
    project's ``pyproject.toml``, outside any ``site-packages``."""
    here = Path(package_file or __file__).resolve()
    if "site-packages" in here.parts or "dist-packages" in here.parts:
        return False
    try:
        return (here.parents[2] / "pyproject.toml").is_file() and here.parents[1].name == "src"
    except IndexError:
        return False


def _pytest_running() -> bool:
    return "_pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ


def decide(
    *,
    home: str,
    real_home: str,
    kiln_home: str | None,
    pytest_running: bool,
    sandbox_requested: bool,
    source_checkout: bool,
    allow_real_home: bool,
) -> str | None:
    """Why this process must not use the real home (``"sandbox"`` or
    ``"test"``), or ``None`` when it may.  Pure, so every case is tested
    without a real home in reach."""
    if allow_real_home:
        return None
    points_at_real = _inside(home, real_home) or bool(kiln_home and _inside(kiln_home, real_home))
    if not points_at_real:
        return None
    if sandbox_requested:
        return "sandbox"
    if pytest_running and source_checkout:
        return "test"
    return None


def protect(*, source_checkout: bool | None = None) -> str | None:
    """Move this process's home to a scratch folder when :func:`decide` says
    it must not use the real one.  Returns the new home, or ``None`` when
    nothing moved.  Never raises: a failure here leaves the process as it was
    and says so in the log.

    ``source_checkout`` lets a package that imports Kiln say whether IT runs
    from a developer's tree; unset, Kiln's own location answers."""
    try:
        if source_checkout is None:
            source_checkout = is_source_checkout()
        reason = decide(
            home=os.environ.get("HOME", ""),
            real_home=account_home(),
            kiln_home=os.environ.get("KILN_HOME") or None,
            pytest_running=_pytest_running(),
            sandbox_requested=_flag(SANDBOX_ENV),
            source_checkout=source_checkout,
            allow_real_home=_flag(ALLOW_REAL_HOME_ENV),
        )
        if reason is None:
            return None
        import site

        # The interpreter found its per-user packages from the old HOME;
        # every child it starts would look under the new one and lose them.
        os.environ.setdefault("PYTHONUSERBASE", site.getuserbase())
        scratch = tempfile.mkdtemp(prefix=f"kiln-{reason}-home-")
        os.makedirs(os.path.join(scratch, ".kiln"), exist_ok=True)
        os.environ["HOME"] = scratch
        if os.environ.get("KILN_HOME"):
            os.environ["KILN_HOME"] = os.path.join(scratch, ".kiln")
        os.environ[MOVED_ENV] = scratch
        logger.warning(
            "Kiln moved this %s run's home to %s: it never writes your real ~/.kiln "
            "(set %s=1 to use the real records on purpose).",
            "sandboxed" if reason == "sandbox" else "test", scratch, ALLOW_REAL_HOME_ENV,
        )
        return scratch
    except Exception:  # noqa: BLE001 — the guard must never break an import
        logger.warning("Kiln could not check whether this run may use the real ~/.kiln", exc_info=True)
        return None


__all__ = [
    "ALLOW_REAL_HOME_ENV",
    "MOVED_ENV",
    "SANDBOX_ENV",
    "account_home",
    "decide",
    "is_source_checkout",
    "protect",
]
