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

import os

#: Set process-wide in ``fly.toml`` on the hosted deploy.  Absent on a
#: user's own install, which is exactly what makes it a reliable signal.
HOSTED_ENV_VAR = "KILN_HOSTED_MULTITENANT"

_TRUTHY = frozenset({"1", "true", "yes"})


def is_hosted_multitenant() -> bool:
    """True on the hosted multi-tenant deploy, False on a user's machine.

    Defaults to False: an unset variable means "somebody's own install,"
    which is the safe reading — a local install told it's hosted would
    hide its own actionable fix-it instructions.
    """
    return os.environ.get(HOSTED_ENV_VAR, "").strip().lower() in _TRUTHY
