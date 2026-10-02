"""``kiln consent`` — the commands a person types to open, see, extend and
close a standing window.

A yes is for one print.  When a person wants an unattended agent to start
prints for a while, they say so here, on purpose, at a terminal:

    kiln consent window --for 2h --printer garage
    kiln consent window --for 30m --printers garage,workshop
    kiln consent window --for 1h --fleet
    kiln consent window --always --printer garage
    kiln consent status
    kiln consent extend w_1a2b3c4d5e6f --for 1h
    kiln consent revoke w_1a2b3c4d5e6f      (or --all)

``window`` and ``extend`` refuse unless stdin and stdout are both a
terminal.  There is no flag that stands in for the person — an agent can
type a flag.  ``revoke`` works from anywhere: closing is the safe
direction.  Nothing here runs on the hosted server, where the file under
``~/.kiln`` is nobody's.  See :mod:`kiln.consent_windows`.

The terminal is one of the doors.  A person who never opens one gets a
window from the approval dialog their assistant's app draws before a
print — "yes, and for the next 2 hours", a length they type, every
printer on the fleet tier — and closes it by telling the assistant
(``revoke_consent_window``).  ``status`` shows which door opened each
window.  A named list of printers is this command's alone; the cap (24h)
is the same at every door.

``--always`` is the window with no end, for one printer: *always allow*.
It is this command's alone — no dialog, no typed code and no tool turns
it on — and it asks the person to type the printer's name before it
does.  It turns itself off if a different machine is later set up under
that name.  ``revoke`` turns it off like any window.
"""

from __future__ import annotations

import json
import sys

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
    """The same description the status tool and a print result carry."""
    return consent_windows.describe(w)


@click.group("consent")
def consent() -> None:
    """A person's standing yes: open, see, extend or close a window."""


def always_allow_screen(name: str) -> str:
    """What a person reads before they turn always allow on for *name*."""
    return (
        f"Always allow prints on {name}?\n"
        "\n"
        "Your assistant will start prints on this printer as soon as it's asked. "
        "Kiln won't check with you first.\n"
        "\n"
        "Before you say yes:\n"
        "- A print can start when nobody is there to watch it.\n"
        "- Anyone who can message your assistant can start one.\n"
        "- Kiln can't always see whether the last print is still on the bed.\n"
        "\n"
        "What stays the same:\n"
        "- Kiln's safety checks run before every print.\n"
        "- You can stop any print at any time.\n"
        "- You or your assistant can turn this off at any time.\n"
    )


def _turn_on_always(
    ctx: click.Context, printer: str | None, printers: str | None, fleet: bool, json_mode: bool,
) -> None:
    """``kiln consent window --always``: show the screen, take the typed
    name, turn it on.  The engine (:func:`consent_windows.open_always`)
    holds every rule again; this is only the asking."""
    if fleet or printers:
        click.echo(format_error(
            "always allow is for one printer: name it with --printer NAME. Every printer at once "
            "is a window with an end (--for 2h --fleet).",
            code="CONSENT_INVALID", json_mode=json_mode,
        ))
        sys.exit(2)
    scope = _resolve_scope(ctx, printer, None, False, json_mode)
    name = scope[0]
    if not consent_windows.person_at_terminal():
        _refused(consent_windows.NotAPerson(
            "always allow is turned on by a person at a terminal (stdin and stdout both a TTY); "
            "nothing else can turn it on"
        ), json_mode)
        return
    click.echo(always_allow_screen(name))
    typed = click.prompt("Type the printer's name to turn it on", default="", show_default=False)
    try:
        w = consent_windows.open_always(printer_name=name, typed_name=typed)
    except (consent_windows.NotAPerson, ValueError) as exc:
        _refused(exc, json_mode)
        return
    if json_mode:
        click.echo(json.dumps({"success": True, "window": _row(w)}))
        return
    click.echo(f"Always allow is on for {name}. Kiln will start prints on it without asking.")
    click.echo(f"Turn it off at any time: tell your assistant \"ask me first\", or run: kiln consent revoke {w.id}")


@consent.command("window")
@click.option("--for", "duration", default=None, help="How long, like 2h, 30m or 1d (24h at most).")
@click.option(
    "--always", "always", is_flag=True,
    help="No end, for one printer: always allow prints on it. You type the printer's name to turn it on.",
)
@click.option("--printer", default=None, help="One printer the window covers.")
@click.option("--printers", default=None, help="Several, comma-separated.")
@click.option("--fleet", is_flag=True, help="Every printer.")
@click.option("--json", "json_mode", is_flag=True, help="Machine-readable output.")
@click.pass_context
def window(
    ctx: click.Context, duration: str | None, always: bool, printer: str | None, printers: str | None,
    fleet: bool, json_mode: bool,
) -> None:
    """Open a standing window: prints may start on the named printer(s)
    for the next while without asking you each time.

    Refused unless you are at a terminal.  With no scope flag the window
    covers the one printer a print would be aimed at.  The preview rule
    still applies to every print inside the window.

    With --always instead of --for, the window has no end and covers one
    printer: Kiln asks you to type the printer's name, then starts prints
    on it without asking until you turn it off (kiln consent revoke).
    """
    if always and duration:
        click.echo(format_error(
            "say it one way: --for a length, or --always for no end.",
            code="CONSENT_INVALID", json_mode=json_mode,
        ))
        sys.exit(2)
    if always:
        _turn_on_always(ctx, printer, printers, fleet, json_mode)
        return
    if not duration:
        click.echo(format_error(
            "say how long: --for 2h (24h at most), or --always for one printer with no end.",
            code="CONSENT_INVALID", json_mode=json_mode,
        ))
        sys.exit(2)
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
    # Checked against the machine first, so status never lists always
    # allow that the next print would find closed.
    live = consent_windows.standing_now()
    closed = consent_windows.turned_off_recently()
    if json_mode:
        click.echo(json.dumps({
            "success": True, "windows": [_row(w) for w in live],
            "turned_off": [_row(w) for w in closed],
        }))
        return
    if not live:
        click.echo("No standing window is open: every print asks you.")
    for w in live:
        row = _row(w)
        if row["always"]:
            click.echo(
                f"{w.id}  Always allow is on for {row['scope']}: prints start without asking.  "
                f"Turned on {row['set_at']} by {w.set_by} via {row['opened_via']}"
            )
            continue
        click.echo(
            f"{w.id}  {row['scope']}  until {row['until']} ({row['remaining_minutes']} min)  "
            f"set by {w.set_by} via {row['opened_via']}"
            + (f"  extended x{row['extensions']}" if row["extensions"] else "")
        )
    for w in closed:
        from kiln.consent_window_note import turned_off_line

        click.echo(turned_off_line(consent_windows.describe_scope(w.scope)))


@consent.command("extend")
@click.argument("window_id")
@click.option("--for", "duration", required=True, help="Open for this long from now, like 1h (24h at most).")
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
        if w.always:
            click.echo(
                f"Always allow is off for {consent_windows.describe_scope(w.scope)}. "
                "Kiln will ask before each print."
            )
            continue
        click.echo(f"Window {w.id} closed. Prints ask you again.")


def register_consent_cli(cli_group: click.Group) -> None:
    """Attach ``kiln consent {window,status,extend,revoke}``."""
    cli_group.add_command(consent)
