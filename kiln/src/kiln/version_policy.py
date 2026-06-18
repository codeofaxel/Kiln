"""Version-awareness policy — turn a client version into a clear verdict.

This is the brain behind Kiln's "you should update" experience.  Given the
running version and what the world expects of it (the latest on PyPI, an
optional *recommended* floor, an optional *required* floor), it returns one
verdict with everything a surface needs to act:

    ok          — current enough; say nothing.
    available   — a newer Kiln exists; a gentle, optional nudge.
    recommended — you're behind a version we'd really like you on; firmer.
    required    — below a hard floor; new work is blocked until you update.

The verdict is deliberately phrased as an OFFER, not an instruction.  Kiln
does the work for the user: the agent reads ``verdict.offer`` and asks
"want me to update it for you now?" — it never makes the user copy a pip
command (the literal command is still carried in ``verdict.command`` for
non-agent surfaces and for full transparency).

This module is pure and dependency-light: it compares versions via
:func:`kiln.version_check.is_newer` and holds no state, so both the client
(self-check against PyPI) and the hosted server (check the caller's version
against its configured floors) share exactly one notion of "what does this
version mean."  It NEVER upgrades anything and NEVER decides to block on a
version it can't parse — an unknown version is always ``ok``.
"""

from __future__ import annotations

from dataclasses import dataclass

from kiln.version_check import UPGRADE_COMMAND, is_newer

# Verdict states, ordered from quietest to loudest.
OK = "ok"
AVAILABLE = "available"
RECOMMENDED = "recommended"
REQUIRED = "required"


@dataclass(frozen=True)
class UpgradeVerdict:
    """A single, surface-agnostic answer to "is this version current enough?".

    ``offer`` is the line an agent speaks to the user; ``command`` is the
    literal pip one-liner for non-agent surfaces.  ``blocking`` is True only
    for ``required`` — the one state where new work should be refused until
    the user updates.
    """

    state: str
    current: str
    target: str | None
    blocking: bool
    headline: str
    offer: str | None
    command: str
    reason: str | None = None

    def to_block(self) -> dict:
        """Compact JSON for API responses / the ``update`` block of tools."""
        block: dict = {
            "state": self.state,
            "current": self.current,
            "target": self.target,
            "blocking": self.blocking,
            "headline": self.headline,
            "command": self.command,
        }
        if self.offer:
            block["offer"] = self.offer
        if self.reason:
            block["reason"] = self.reason
        return block


def _ok(current: str) -> UpgradeVerdict:
    return UpgradeVerdict(
        state=OK,
        current=current,
        target=None,
        blocking=False,
        headline="Kiln is up to date.",
        offer=None,
        command=UPGRADE_COMMAND,
    )


def evaluate(
    current: str,
    *,
    latest: str | None = None,
    recommended: str | None = None,
    floor: str | None = None,
    reason: str | None = None,
) -> UpgradeVerdict:
    """Return the verdict for ``current`` against the known expectations.

    Precedence is strict-to-soft: a ``floor`` breach (``required``) wins over a
    ``recommended`` breach, which wins over merely-not-``latest`` (``available``).
    An unknown / unparseable ``current`` always returns ``ok`` — Kiln never
    nudges, and certainly never blocks, on a version it can't reason about.

    The ``target`` is the version we steer the user toward: the latest known
    release when we have it, otherwise the floor that was breached — so the
    user always lands on something at least as new as what's being asked of
    them.
    """
    current = (current or "").strip()
    if not current or current == "unknown":
        return _ok(current or "unknown")

    # The newest thing we can name to steer the user toward.
    target = latest if (latest and is_newer(latest, current)) else None

    # required — below a hard floor. New work is blocked until they update.
    if floor and is_newer(floor, current):
        target = target or floor
        why = reason or "a newer Kiln is required to continue"
        return UpgradeVerdict(
            state=REQUIRED,
            current=current,
            target=target,
            blocking=True,
            headline=f"Kiln {target} is required to continue (you're on {current}).",
            offer=(
                f"This needs Kiln {target} to go further — {why}. "
                "Want me to update it for you now? It takes a few seconds, then "
                "one quick restart and I'll pick up right where we left off."
            ),
            command=UPGRADE_COMMAND,
            reason=why,
        )

    # recommended — behind a version we'd really like them on.
    if recommended and is_newer(recommended, current):
        target = target or recommended
        return UpgradeVerdict(
            state=RECOMMENDED,
            current=current,
            target=target,
            blocking=False,
            headline=f"Kiln {target} is out and worth getting (you're on {current}).",
            offer=(
                f"Kiln {target} is available and includes updates worth having. "
                "Want me to update it for you now? A few seconds plus one restart."
            ),
            command=UPGRADE_COMMAND,
            reason=reason,
        )

    # available — simply not the latest. The quietest, fully-optional nudge.
    if target:
        return UpgradeVerdict(
            state=AVAILABLE,
            current=current,
            target=target,
            blocking=False,
            headline=f"Kiln {target} is available (you're on {current}).",
            offer=(
                f"A newer Kiln ({target}) is out. Happy to update it for you "
                "whenever you like — just say the word."
            ),
            command=UPGRADE_COMMAND,
        )

    return _ok(current)
