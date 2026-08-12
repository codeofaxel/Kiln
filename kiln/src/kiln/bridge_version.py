"""Is the bridge daemon running the Kiln you actually have installed?

A bridge started by ``kiln bridge enable`` is ``<python> -m kiln.bridge_client``
under launchd, systemd or a Windows Run key — the pip-installed package, run by
the operating system, for weeks at a time.  Both real supervisors restart a
bridge that **dies**; neither restarts one that is merely **old**.  So
``pip install --upgrade kiln3d`` lands new files on disk and the daemon keeps
serving the modules it imported at boot, indefinitely, with nothing amiss to
see: it is connected, it answers, it prints.

Nothing could tell you.  The relay is shown the version in the daemon's
handshake — the running one.  Every command you type reports the version it
just imported — the installed one.  Both numbers are correct, they describe one
machine, and no surface anywhere compared them.  That is the gap this module
closes: it is the one place that holds both, and it says which of the two
actions is owed.

**It never installs anything, and that is a decision, not an omission.**

* Nothing else in Kiln updates itself.  :mod:`kiln.version_check` is advisory,
  :mod:`kiln.self_update` runs only on an explicit ``confirm``, and the agent
  nudge is worded as a question.  A self-upgrading bridge would be the only
  part of Kiln that changes software you installed without asking, and you
  would meet it as "something updated itself" with no memory of allowing it.
  ``kiln bridge start`` already refuses the smaller version of that surprise:
  it will not install a login item you did not ask for.
* The one place an update could be applied at a safe moment is
  :mod:`kiln.bridge_supervisor`, between one bridge process and the next — and
  the supervisor is absent on exactly the two platforms where a bridge runs for
  weeks unattended, because launchd and systemd supervise the bridge directly.
  Auto-update built there would reach the session-only case, where somebody is
  at the keyboard and one command does it, and miss the case that motivates it.
* ``pip`` rewrites files under a live process.  The bridge imports lazily all
  over (``websockets`` in the run loop, ``kiln.server`` in the tool caller,
  ``yaml`` in the credential read), so an in-place upgrade can load new code
  into an old session.  The only coherent way to apply one is a restart — which
  means taking the user's printer off the web at a moment we chose rather than
  one they did.
* The costs are not symmetric.  A nudge nobody reads costs a slightly old
  bridge, which the web already flags.  An upgrade that goes wrong costs a
  broken Python environment on a machine whose owner may be a thousand miles
  away, and it breaks it by way of the one channel that reached it.

So: say it clearly, in the place the operator looks, and let them choose.
``tests/test_bridge_version.py`` pins the no-installer part structurally, so
adding one has to be a conscious act rather than a quiet afternoon's work.

**Code signing does not apply here.**  Recorded because the question keeps
being asked of this daemon: there is no compiled binary in this picture to
sign.  The bridge is the ``kiln3d`` package running under the user's own
interpreter, so its integrity story is the one PyPI already provides for the
package — the same artifact, verified the same way, whether it is being run by
a person or by launchd.
"""

from __future__ import annotations

from dataclasses import dataclass

from kiln.version_check import UPGRADE_COMMAND, is_newer

# Verdicts, quietest first.
CURRENT = "current"
#: The newer Kiln is already on disk; only the daemon is behind.  Needs no
#: network to detect and no download to fix — the most actionable of the three.
RESTART_PENDING = "restart-pending"
#: A newer Kiln exists on PyPI than this machine has.  Fixing it also implies
#: the restart above, since installing alone never reaches the running daemon.
UPDATE_AVAILABLE = "update-available"


@dataclass(frozen=True)
class BridgeVersionVerdict:
    """What to say about the bridge's version, or nothing at all.

    ``lines`` is empty exactly when ``state`` is :data:`CURRENT`, so a caller
    can render unconditionally without first asking whether there is news.
    """

    state: str
    lines: tuple[str, ...]


#: One verb, unconditionally.  This advice used to branch — ``disable &&
#: enable`` for a login-managed bridge, ``stop && start`` for a session one —
#: because no single command restarted a bridge, and the wrong pair does
#: nothing at all (``kiln bridge start`` refuses on a login-managed bridge).
#: Two commands to do one thing, and a branch to pick which two.  The fix was
#: not better wording, it was the missing verb: ``kiln bridge restart`` works
#: out how this bridge is supervised so nobody reading this has to.
RESTART_COMMAND = "kiln bridge restart"


def describe(
    *,
    running: str | None,
    installed: str,
    latest: str | None = None,
) -> BridgeVersionVerdict:
    """Compare what the daemon loaded against what exists, and say what is owed.

    *running* is the version the live bridge process imported at boot, read
    back from its state file; *installed* is what ``import kiln`` yields right
    now, in this freshly-started process; *latest* is the newest published
    release when it is known, and ``None`` when it is not — a cold cache, or a
    user who turned update checks off.  Only *latest* needs the network, which
    is why the sharpest signal here is the one that works offline.

    A *running* we cannot read or cannot parse yields no restart claim.  An old
    bridge daemon predating this file records no version at all, and an honest
    silence beats a confident guess about which of two numbers is newer.
    """
    running = (running or "").strip()

    # `is_newer` returns False for anything it cannot parse, so an unreadable
    # version on either side degrades to "no news" rather than a wrong verdict.
    needs_install = bool(latest) and is_newer(latest, installed)
    needs_restart = bool(running) and is_newer(installed, running)

    if not needs_install and not needs_restart:
        return BridgeVersionVerdict(state=CURRENT, lines=())

    if not needs_install:
        return BridgeVersionVerdict(
            state=RESTART_PENDING,
            lines=(
                f"Running Kiln {running}, but {installed} is installed here. "
                "A bridge keeps the version it started with.",
                f"Pick up the newer one: {RESTART_COMMAND}",
            ),
        )

    # An update is owed on disk too.  Installing without restarting leaves the
    # daemon exactly where it is, so the two steps are given as one line.
    if needs_restart:
        behind = (
            f"the bridge is running {running}, and {installed} is already "
            "installed here"
        )
    elif running:
        behind = f"the bridge is running {running}"
    else:
        behind = f"this machine has {installed}"
    return BridgeVersionVerdict(
        state=UPDATE_AVAILABLE,
        lines=(
            f"Kiln {latest} is available ({behind}).",
            # Two genuinely different actions — fetch it, then pick it up —
            # chained so they are still one paste.  `&&` is the honest join:
            # if the install fails there is nothing new to restart into.
            f"Update and pick it up: {UPGRADE_COMMAND} && {RESTART_COMMAND}",
        ),
    )
