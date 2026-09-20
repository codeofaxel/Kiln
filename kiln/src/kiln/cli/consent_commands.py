"""``kiln consent`` — the commands a person types to open, see, extend and
close a standing window.

A yes is for one print.  When a person wants an unattended agent to start
prints for a while, they say so here, on purpose, at a terminal:

    kiln consent window --for 2h --printer garage
    kiln consent window --for 30m --printers garage,workshop
    kiln consent window --for 1h --fleet
    kiln consent status
    kiln consent extend w_1a2b3c4d5e6f --for 1h
    kiln consent revoke w_1a2b3c4d5e6f      (or --all)

``window`` and ``extend`` refuse unless stdin and stdout are both a
terminal.  There is no flag that stands in for the person — an agent can
type a flag.  ``revoke`` works from anywhere: closing is the safe
direction.  Nothing here runs on the hosted server, where the file under
``~/.kiln`` is nobody's.  See :mod:`kiln.consent_windows`.
"""

from __future__ import annotations

import json
import sys
import time

import click

from kiln import consent_windows
from kiln.cli.output import format_error


def _configured_printers() -> list[dict]:
    """``[{name, active}]`` from the CLI's own config; ``[]`` when none."""
    try:
        from kiln.cli.config import list_printers

        return list(list_printers())
    except Exception:  # noqa: BLE001 — no config is no printers
        return []


def _default_scope(ctx: click.Context) -> tuple[str, ...] | None:
    """The one printer a print would be aimed at: ``--printer`` on the
    root command, else the active configured printer, else the only one."""
    aimed = (ctx.obj or {}).get("printer") if ctx.obj else None
    if aimed:
        return (str(aimed),)
    printers = _configured_printers()
    active = [p["name"] for p in printers if p.get("active")]
    if active:
        return (str(active[0]),)
    if len(printers) == 1:
        return (str(printers[0]["name"]),)
    return None


def _resolve_scope(
    ctx: click.Context, printer: str | None, printers: str | None, fleet: bool, json_mode: bool,
):
    chosen = sum(1 for flag in (printer, printers, fleet) if flag)
    if chosen > 1:
        click.echo(format_error(
            "name the scope one way: --printer NAME, --printers A,B or --fleet.",
            code="CONSENT_SCOPE_AMBIGUOUS", json_mode=json_mode,
        ))
        sys.exit(2)
    if fleet:
        return consent_windows.SCOPE_FLEET
    if printers:
        names = tuple(n.strip() for n in printers.split(",") if n.strip())
        if names:
            return names
    if printer:
        return (printer.strip(),)
    scope = _default_scope(ctx)
    if scope is None:
        click.echo(format_error(
            "no printer to aim this at: name it with --printer NAME, --printers A,B or --fleet.",
            code="CONSENT_SCOPE_MISSING", json_mode=json_mode,
        ))
        sys.exit(2)
    return scope


def _refused(exc: Exception, json_mode: bool) -> None:
    code = "CONSENT_NOT_A_PERSON" if isinstance(exc, consent_windows.NotAPerson) else "CONSENT_INVALID"
    click.echo(format_error(str(exc), code=code, json_mode=json_mode))
    sys.exit(1)


def _row(w: consent_windows.Window) -> dict:
    now = time.time()
    return {
        "id": w.id,
        "scope": consent_windows.describe_scope(w.scope),
        "set_by": w.set_by,
        "set_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(w.set_at)),
        "until": time.strftime("%Y-%m-%d %H:%M", time.localtime(w.until)),
        "remaining_minutes": max(0, int((w.until - now) // 60)),
        "live": w.live(now),
        "revoked": w.revoked_at is not None,
        "extensions": len(w.extensions),
    }


@click.group("consent")
def consent() -> None:
    """A person's standing yes: open, see, extend or close a window."""


@consent.command("window")
@click.option("--for", "duration", required=True, help="How long, like 2h, 30m or 1d.")
@click.option("--printer", default=None, help="One printer the window covers.")
@click.option("--printers", default=None, help="Several, comma-separated.")
@click.option("--fleet", is_flag=True, help="Every printer.")
@click.option("--json", "json_mode", is_flag=True, help="Machine-readable output.")
@click.pass_context
def window(
    ctx: click.Context, duration: str, printer: str | None, printers: str | None, fleet: bool, json_mode: bool,
) -> None:
    """Open a standing window: prints may start on the named printer(s)
    for the next while without asking you each time.

    Refused unless you are at a terminal.  With no scope flag the window
    covers the one printer a print would be aimed at.  The preview rule
    still applies to every print inside the window.
    """
    scope = _resolve_scope(ctx, printer, printers, fleet, json_mode)
    try:
        seconds = consent_windows.parse_duration(duration)
        w = consent_windows.open_window(seconds=seconds, scope=scope)
    except (consent_windows.NotAPerson, consent_windows.NotTheFleetTier, ValueError) as exc:
        _refused(exc, json_mode)
        return
    row = _row(w)
    if json_mode:
        click.echo(json.dumps({"success": True, "window": row}))
        return
    click.echo(
        f"Standing window {w.id} open for {row['scope']} until {row['until']} "
        f"({row['remaining_minutes']} min), set by {w.set_by}."
    )
    click.echo("Prints inside it still need a preview on record. Close it early with: "
               f"kiln consent revoke {w.id}")


@consent.command("status")
@click.option("--json", "json_mode", is_flag=True, help="Machine-readable output.")
def status(json_mode: bool) -> None:
    """Which standing windows are open, for what, and until when."""
    live = consent_windows.live_windows()
    if json_mode:
        click.echo(json.dumps({"success": True, "windows": [_row(w) for w in live]}))
        return
    if not live:
        click.echo("No standing window is open: every print asks you.")
        return
    for w in live:
        row = _row(w)
        click.echo(
            f"{w.id}  {row['scope']}  until {row['until']} ({row['remaining_minutes']} min)  "
            f"set by {w.set_by}"
            + (f"  extended x{row['extensions']}" if row["extensions"] else "")
        )


@consent.command("extend")
@click.argument("window_id")
@click.option("--for", "duration", required=True, help="Open for this long from now, like 1h.")
@click.option("--json", "json_mode", is_flag=True, help="Machine-readable output.")
def extend(window_id: str, duration: str, json_mode: bool) -> None:
    """Keep a window open longer: its end becomes now plus the duration.
    Refused unless you are at a terminal."""
    try:
        seconds = consent_windows.parse_duration(duration)
        w = consent_windows.extend_window(window_id, seconds=seconds)
    except (consent_windows.NotAPerson, consent_windows.NotTheFleetTier, ValueError) as exc:
        _refused(exc, json_mode)
        return
    except KeyError:
        click.echo(format_error(f"no window {window_id}", code="CONSENT_NO_SUCH_WINDOW", json_mode=json_mode))
        sys.exit(1)
    row = _row(w)
    if json_mode:
        click.echo(json.dumps({"success": True, "window": row}))
        return
    click.echo(f"Window {w.id} now open until {row['until']} ({row['remaining_minutes']} min).")


@consent.command("revoke")
@click.argument("window_id", required=False)
@click.option("--all", "everything", is_flag=True, help="Close every open window.")
@click.option("--json", "json_mode", is_flag=True, help="Machine-readable output.")
def revoke(window_id: str | None, everything: bool, json_mode: bool) -> None:
    """Close a standing window now.  Jobs queued under it will not start."""
    if everything:
        closed = consent_windows.revoke_all()
    elif window_id:
        try:
            closed = [consent_windows.revoke_window(window_id)]
        except KeyError:
            click.echo(format_error(f"no window {window_id}", code="CONSENT_NO_SUCH_WINDOW", json_mode=json_mode))
            sys.exit(1)
    else:
        click.echo(format_error("say which: a window id, or --all.", code="CONSENT_NO_SUCH_WINDOW", json_mode=json_mode))
        sys.exit(2)
    if json_mode:
        click.echo(json.dumps({"success": True, "revoked": [w.id for w in closed]}))
        return
    if not closed:
        click.echo("No open window to close.")
        return
    for w in closed:
        click.echo(f"Window {w.id} closed. Prints ask you again.")


def register_consent_cli(cli_group: click.Group) -> None:
    """Attach ``kiln consent {window,status,extend,revoke}``."""
    cli_group.add_command(consent)
