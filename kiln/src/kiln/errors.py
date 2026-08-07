"""Typed refusals shared across Kiln's tool surface.

One class lives here so far, on purpose: a refusal that must survive the
tool layer's broad exception handling as a recognizable TYPE, not just a
message.  Modules that need it import from here rather than defining their
own copy — two spellings of the same refusal is how one of them drifts.
"""

from __future__ import annotations


class HostedUnavailableError(ValueError):
    """A capability needs the caller's own machine, not this server.

    The hosted multi-tenant deploy (api.kiln3d.com) runs one process and
    one ``~/.kiln`` for every customer, so state keyed by caller-chosen
    names — a design library, timelapse frames — cannot be answered from
    there: two customers who pick the same name share one file, and the
    box keeps no persistent volume anyway.  Resolvers for such stores
    refuse on that deploy, and they raise this.

    Subclasses ``ValueError`` deliberately.  The tools over these stores
    already funnel ``ValueError`` into their ``{"ok": False, "error": ...}``
    envelope, so the refusal keeps reaching the caller as a stated reason
    with no per-tool branch.  What the subclass adds is a NAME: a handler
    can catch this explicitly before its generic ``except Exception``, and
    a gate can verify that it does — the difference between "the refusal
    happens to survive today's handlers" and "the refusal is contracted to
    survive them."
    """
