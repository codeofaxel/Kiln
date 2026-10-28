"""The line a person types to run a Kiln command on this computer.

Kiln often runs without a ``kiln`` on the person's PATH: a plugin starts it
through uvx, or it lives in a virtualenv nobody activated.  A message telling
them to run ``kiln accept-terms`` then names a command their terminal does not
have, on the first thing a new install asks of them.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys

# Set by the program that launches Kiln when it knows the right command better
# than Kiln can guess, e.g. "uvx --from kiln3d==1.5.0 kiln".
CLI_ENV = "KILN_CLI"

_ON_WINDOWS = os.name == "nt"


def _quoted(path: str) -> str:
    """A path quoted the way this computer's shell reads it."""
    return subprocess.list2cmdline([path]) if _ON_WINDOWS else shlex.quote(path)


def kiln_command(subcommand: str, *values: str) -> str:
    """``kiln <subcommand> [values...]`` as a line that runs on this computer.

    ``KILN_CLI`` wins when set.  Otherwise ``kiln`` when one is on PATH, and
    failing that this interpreter's ``-m kiln``, which always reaches the
    install that is running.

    *subcommand* is Kiln's own words, as typed.  Each of *values* (a printer
    or file name, an id, anything a person or a printer chose) is quoted for
    this computer's shell, so "Garage A1" stays one argument.
    """
    base = os.environ.get(CLI_ENV, "").strip()
    if not base:
        base = "kiln" if shutil.which("kiln") else f"{_quoted(sys.executable)} -m kiln"
    return " ".join([f"{base} {subcommand}", *(_value(str(v)) for v in values)])


# What a value may carry into a line meant to be pasted: plain ASCII letters,
# digits, spaces and a few separators, and never a leading "-" (it would read
# as an option).  Anything else, in any shell, is shown as a placeholder: an
# allowlist, because each shell (cmd, PowerShell, sh) acts on different
# characters and a terminal acts on invisible ones.
_SAFE_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._:/@+=,-]*")
_PLACEHOLDER = "<name>"


def _value(value: str) -> str:
    """A person's or a printer's value, quoted, or a placeholder when it holds
    anything a shell or a terminal could act on."""
    if not _SAFE_VALUE.fullmatch(value):
        return _PLACEHOLDER
    return _quoted(value)
