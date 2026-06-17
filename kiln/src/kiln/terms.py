"""Terms of use acceptance tracking.

Stores acceptance state in the SQLite settings table.  The current terms
version is bumped whenever TERMS_OF_USE.md changes materially; a version mismatch
triggers re-acceptance during ``kiln setup``.
"""

from __future__ import annotations

import time

_CURRENT_TERMS_VERSION = "3.0"

_SETTINGS_KEY_VERSION = "terms_accepted_version"
_SETTINGS_KEY_TIMESTAMP = "terms_accepted_at"

_TERMS_SUMMARY = """\
  By using Kiln, you're agreeing to a few things:

  1. Safety stays with you. Kiln's checks lower the risk of a print
     going wrong — they don't remove it. Supervise what an AI agent
     runs on your printer, and don't run prints unattended without
     smoke/fire precautions.
  2. What you make is yours — and your responsibility. You own your
     designs and outputs, and you're responsible for following the
     laws that apply to you. Kiln itself doesn't monitor or restrict
     your files, though the AI assistant you use may decline a
     request under its own policies.
  3. Free and Pro are for personal projects. Selling what you print —
     or fulfilling client and custom orders — is a Business-tier
     feature.
  4. Fees are shown up front. Fulfillment orders carry a 5%
     orchestration fee (min $0.25, max $200); your first 3 each month
     are free. Printing on your own printer is always free.
  5. Third parties set their own rules. Marketplaces and fulfillment
     partners are governed by their terms, not Kiln's.
  6. Kiln is provided "as is", without warranty.

  Please read the full Terms before you accept: https://kiln3d.com/terms
  Privacy policy: https://kiln3d.com/privacy"""


# Forcing function: this marker MUST equal _CURRENT_TERMS_VERSION (enforced by
# test_summary_reviewed_for_current_version).  When you bump the terms version,
# that test stays red until you have re-read _TERMS_SUMMARY above AND the
# matching acceptance copy on the other surfaces -- the web sign-up and the MCP
# first-run gate -- updated whatever materially changed, then set this to match.
# It makes "did we refresh every place the user accepts the terms?" a conscious
# step on every change instead of something we remember to do by luck.
_SUMMARY_REVIEWED_FOR_VERSION = "3.0"


def get_accepted_version(*, db=None) -> str | None:
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
