"""``kiln consent`` — the commands a person types to open, see, extend and
close a standing window.

A yes is for one print.  When a person wants an unattended agent to start
prints for a while, they say so here, on purpose, at a terminal:

    kiln consent window --for 2h --printer garage
    kiln consent window --for 30m --printers garage,workshop
    kiln consent window --for 1h --fleet
    kiln consent window --always --printer garage
    kiln consent window --always --printers garage,workshop,attic
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
that name.  ``revoke`` turns it off like any window.  With ``--printers
A,B,C`` it is turned on for each printer named, one entry apiece, after
the person types how many they named; that form is the fleet tier's.
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


#: The bed line on the screen: what Kiln does where it can look, and the
#: plain fact where it cannot.
_BED_WITH_CAMERA = (
    "- Kiln looks at the bed through the camera before every print. Your assistant judges the "
    "picture, and Kiln asks you if it can't see the bed."
)
_BED_NO_CAMERA = "- {names} {have} no camera Kiln can use, so Kiln can't check the bed before it prints."


def always_allow_screen(cameras: dict[str, bool]) -> str:
    """What a person reads before they turn always allow on.  *cameras*
    is ``{printer name: whether Kiln can look at its bed}``, for the one
    printer or for each of several."""
    names = list(cameras)
    several = len(names) > 1
    if several:
        head = f"Always allow prints on these {len(names)} printers?\n\n" + "".join(f"  {n}\n" for n in names)
        what = "Your assistant will start prints on each of them as soon as it's asked. "
    else:
        head = f"Always allow prints on {names[0]}?\n"
        what = "Your assistant will start prints on this printer as soon as it's asked. "
    blind = [n for n in names if not cameras[n]]
    bed: list[str] = []
    if blind:
        bed.append(_BED_NO_CAMERA.format(names=", ".join(blind), have="have" if len(blind) > 1 else "has"))
    if len(blind) < len(names):
        # Said of the rest when some printers have no camera, so the line
        # is not read as covering those.
        bed.append(_BED_WITH_CAMERA.replace("- Kiln looks", "- On the rest, Kiln looks") if blind else _BED_WITH_CAMERA)
    return (
        f"{head}\n{what}Kiln won't check with you first.\n"
        "\n"
        "Before you say yes:\n"
        "- A print can start when nobody is there to watch it.\n"
        "- Anyone who can message your assistant can start one.\n"
        + "\n".join(bed) + "\n"
        "\n"
        "What stays the same:\n"
        "- Kiln's safety checks run before every print.\n"
        "- You can stop any print at any time.\n"
        "- You or your assistant can turn this off at any time.\n"
    )


def _has_camera(name: str) -> bool:
    """Whether Kiln can look at this printer's bed -- found out by taking
    a picture now, because printer software that can serve a camera says
    so whether or not one is plugged in.  Never raises."""
    return consent_windows.bed_can_be_seen(name)


def _turn_on_always(
    ctx: click.Context, printer: str | None, printers: str | None, fleet: bool, json_mode: bool,
) -> None:
    """``kiln consent window --always``: show the screen, take what the
    person types, turn it on.  The engine (:func:`consent_windows.open_always`
    and :func:`~consent_windows.open_always_for_several`) holds every rule
    again; this is only the asking."""
    if fleet:
        click.echo(format_error(
            "always allow is turned on for printers you name: --printer NAME, or --printers A,B,C. "
            "Every printer at once is a window with an end (--for 2h --fleet).",
            code="CONSENT_INVALID", json_mode=json_mode,
        ))
        sys.exit(2)
    names = list(_resolve_scope(ctx, printer, printers, False, json_mode))
    if not consent_windows.person_at_terminal():
        _refused(consent_windows.NotAPerson(
            "always allow is turned on by a person at a terminal (stdin and stdout both a TTY); "
            "nothing else can turn it on"
        ), json_mode)
        return
    cameras = {name: _has_camera(name) for name in names}
    # What the screen said, handed to the engine with the names: an entry
    # records "no camera" only for a printer the person read that about.
    blind = [name for name in names if not cameras[name]]
    click.echo(always_allow_screen(cameras))
    try:
        if len(names) == 1:
            typed = click.prompt("Type the printer's name to turn it on", default="", show_default=False)
            opened = [consent_windows.open_always(
                printer_name=names[0], typed_name=typed, told_no_camera=bool(blind),
            )]
        else:
            typed = click.prompt(
                f"Are you sure? Type the number of printers listed ({len(names)}) to turn it on for all of them",
                default="", show_default=False,
            )
            opened = consent_windows.open_always_for_several(
                printer_names=names, typed_count=typed, told_no_camera=blind,
            )
    except (consent_windows.NotAPerson, consent_windows.NotTheFleetTier, ValueError) as exc:
        _refused(exc, json_mode)
        return
    if json_mode:
        click.echo(json.dumps({"success": True, "windows": [_row(w) for w in opened]}))
        return
    where = ", ".join(names)
    click.echo(f"Always allow is on for {where}. Kiln will start prints on {'them' if len(names) > 1 else 'it'} without asking.")
    off = "kiln consent revoke " + (opened[0].id if len(opened) == 1 else "<id>   (kiln consent status lists them)")
    click.echo(f"Turn it off at any time: tell your assistant \"ask me first\", or run: {off}")


@consent.command("window")
@click.option("--for", "duration", default=None, help="How long, like 2h, 30m or 1d (24h at most).")
@click.option(
    "--always", "always", is_flag=True,
    help="No end, for the printer(s) you name: always allow prints there. You confirm by typing.",
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
    Name several with --printers to turn it on for each of them.
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
    # Brought in line with the signed-in account first (always allow can
    # be turned on or off on the account page), then checked against the
    # machine, so status never lists what the next print would find closed.
    consent_windows.sync_account_always()
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
                f"{w.id}  Always allow is on for {row['scope']}: prints start without asking"
                + (", and the bed is not checked first (no camera)" if w.bed_check == consent_windows.BED_NO_CAMERA else "")
                + f".  Turned on {row['set_at']} by {w.set_by} via {row['opened_via']}"
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
