"""What KIND of deployment is this process — one predicate, one answer.

Several subsystems need to know whether they are running on the hosted
multi-tenant server (api.kiln3d.com's Fly box) rather than on a user's own
machine, and they need it for very different reasons: the heartbeat must
not report the shared box as a phantom install, and an error message must
not tell a user to install software on a server they don't control.

Both questions have the SAME answer, so they read the same predicate here
rather than each growing its own copy of the env-var check.  A second copy
is how the two drift.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
from collections.abc import Iterator

#: Set process-wide in ``fly.toml`` on the hosted deploy.  Absent on a
#: user's own install, which is exactly what makes it a reliable signal.
HOSTED_ENV_VAR = "KILN_HOSTED_MULTITENANT"

_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: The answer :func:`hosted_multitenant` sets for the work it wraps.
#: ``None`` means nothing is armed and :data:`HOSTED_ENV_VAR` decides.
_armed: contextvars.ContextVar[bool | None] = contextvars.ContextVar(
    "kiln_runtime_hosted_multitenant", default=None
)


@contextlib.contextmanager
def hosted_multitenant(value: bool = True) -> Iterator[None]:
    """Make :func:`is_hosted_multitenant` answer *value* inside the block.

    A server that answers many accounts from one process wraps each request
    in this, so every check made while the request runs gets the server's
    answer whether or not :data:`HOSTED_ENV_VAR` is set.  The value lives in
    a context variable: code the block awaits, and threads started through
    :func:`asyncio.to_thread`, read it; a :class:`threading.Thread` started
    inside the block reads it only when run in :func:`contextvars.copy_context`.
    It ends with the block, also when the block raises, and a nested block
    restores the outer value.
    """
    token = _armed.set(bool(value))
    try:
        yield
    finally:
        _armed.reset(token)


def is_hosted_multitenant() -> bool:
    """True on the hosted multi-tenant deploy, False on a user's machine.

    Inside a :func:`hosted_multitenant` block, the value the block set.
    Otherwise :data:`HOSTED_ENV_VAR` decides: ``1``, ``true``, ``yes`` or
    ``on``, in any case, reads as hosted.

    Defaults to False: an unset variable means "somebody's own install,"
    which is the safe reading — a local install told it's hosted would
    hide its own actionable fix-it instructions.
    """
    armed = _armed.get()
    if armed is not None:
        return armed
    return os.environ.get(HOSTED_ENV_VAR, "").strip().lower() in _TRUTHY
