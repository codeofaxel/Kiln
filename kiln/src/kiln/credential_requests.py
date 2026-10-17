"""Get a connection need from the printer's own software, for the person.

Some printers' software can hand Kiln a credential the person approves
there, instead of making them find and copy it.  Which needs can be got
this way is data on the need (``ConnectionNeed.request``, see
:mod:`kiln.printer_backends`); the setup doors call the two functions here
rather than knowing any printer's flow:

* :func:`in_terminal` -- a person at a terminal (``kiln setup``, the
  bridge's first-printer offer) is shown where to approve, and the call
  waits.
* :func:`for_agent` -- an agent cannot wait for a person mid-call, so the
  request stays open between calls and each call says where it stands.

Either way, ``None`` or a non-granted state means "ask the person to copy it
as before": a printer that cannot do this is never worse off.
"""

from __future__ import annotations

from typing import Any

import click
import requests

from kiln import octoprint_appkeys
from kiln.printer_backends import ConnectionNeed


def _octoprint_in_terminal(host: str) -> str | None:
    if not octoprint_appkeys.supported(host):
        return None
    if not click.confirm(
        "  Let Kiln ask OctoPrint for a key? You click Allow in OctoPrint", default=True
    ):
        return None
    try:
        request = octoprint_appkeys.start(host)
    except octoprint_appkeys.AppKeyError as exc:
        click.echo(f"  {exc}")
        return None
    click.echo(f"  Now {octoprint_appkeys.how_to_approve(request)}.")
    click.echo("  Waiting for OctoPrint (Ctrl+C to paste a key instead) ", nl=False)
    try:
        key = octoprint_appkeys.wait_for_key(request, on_wait=lambda: click.echo(".", nl=False))
    except KeyboardInterrupt:
        click.echo()
        return None
    except requests.RequestException:
        key = None
    click.echo()
    if key:
        click.echo("  OctoPrint gave Kiln a key.")
        return key
    click.echo("  OctoPrint did not give Kiln a key (it was denied, or the request ran out).")
    return None


def in_terminal(need: ConnectionNeed, host: str) -> str | None:
    """Get *need* from the printer at *host* with the person's approval, or
    ``None`` to ask them for it by hand."""
    if need.request == "octoprint_appkeys" and host:
        return _octoprint_in_terminal(host)
    return None


def for_agent(need: ConnectionNeed, host: str) -> dict[str, Any] | None:
    """Where getting *need* from the printer at *host* stands, starting the
    request if none is open, or ``None`` when *need* cannot be got this way.

    See :func:`kiln.octoprint_appkeys.ask_in_background` for the states.
    """
    if need.request == "octoprint_appkeys" and host:
        return octoprint_appkeys.ask_in_background(host)
    return None
