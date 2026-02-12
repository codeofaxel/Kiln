"""Terms of use acceptance tracking.

Stores acceptance state in the SQLite settings table.  The current terms
version is bumped whenever TERMS.md changes materially; a version mismatch
triggers re-acceptance during ``kiln setup``.
"""

from __future__ import annotations

import time
from typing import Optional

_CURRENT_TERMS_VERSION = "1.0"

_SETTINGS_KEY_VERSION = "terms_accepted_version"
_SETTINGS_KEY_TIMESTAMP = "terms_accepted_at"

_TERMS_SUMMARY = """\
  By using Kiln you agree that:

  1. You are responsible for complying with all applicable laws in your
     jurisdiction.
  2. You are responsible for what you print. Kiln does not monitor,
     filter, or restrict the content of files you print.
  3. You are responsible for printer safety. Kiln's safety systems
     reduce risk but do not eliminate it.
  4. Third-party content (marketplaces, fulfillment) is governed by
     those providers' own terms.
  5. Kiln is provided "as is" without warranty of any kind.

  Full terms: https://github.com/kiln3d/kiln/blob/main/TERMS.md"""


def get_accepted_version(*, db=None) -> Optional[str]:
    """Return the accepted terms version, or ``None`` if never accepted."""
    if db is None:
        from kiln.persistence import get_db
        db = get_db()
    return db.get_setting(_SETTINGS_KEY_VERSION)


def is_current(*, db=None) -> bool:
    """Return ``True`` if the user has accepted the current terms version."""
    return get_accepted_version(db=db) == _CURRENT_TERMS_VERSION


def record_acceptance(*, db=None) -> None:
    """Record that the user accepted the current terms version."""
    if db is None:
        from kiln.persistence import get_db
        db = get_db()
    db.set_setting(_SETTINGS_KEY_VERSION, _CURRENT_TERMS_VERSION)
    db.set_setting(_SETTINGS_KEY_TIMESTAMP, str(time.time()))


def prompt_acceptance() -> bool:
    """Display the terms summary and prompt for acceptance.

    Returns ``True`` if the user accepted, ``False`` otherwise.
    Uses click for consistent CLI prompting.
    """
    import click

    click.echo()
    click.echo(click.style("  Terms of Use", bold=True))
    click.echo(click.style("  ------------", bold=True))
    click.echo(_TERMS_SUMMARY)
    click.echo()
    accepted = click.confirm("  Do you accept these terms?", default=True)
    if accepted:
        record_acceptance()
        click.echo(click.style("  Terms accepted.", fg="green"))
    click.echo()
    return accepted
