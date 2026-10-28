"""The line a person types to run a Kiln command on this computer.

Kiln often runs without a ``kiln`` on the person's PATH: a plugin starts it
through uvx, or it lives in a virtualenv nobody activated.  A message telling
them to run ``kiln accept-terms`` then names a command their terminal does not
have, on the first thing a new install asks of them.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys

# Set by the program that launches Kiln when it knows the right command better
# than Kiln can guess, e.g. "uvx --from kiln3d==1.5.0 kiln".
CLI_ENV = "KILN_CLI"


def kiln_command(subcommand: str) -> str:
    """``kiln <subcommand>`` as a line that runs on this computer.

    ``KILN_CLI`` wins when set.  Otherwise ``kiln`` when one is on PATH, and
    failing that this interpreter's ``-m kiln``, which always reaches the
    install that is running.
    """
    base = os.environ.get(CLI_ENV, "").strip()
    if not base:
        base = "kiln" if shutil.which("kiln") else f"{shlex.quote(sys.executable)} -m kiln"
    return f"{base} {subcommand}"
