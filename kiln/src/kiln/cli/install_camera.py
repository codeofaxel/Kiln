"""``kiln install-camera``: add Kiln's camera add-on from a terminal.

Runs the same install an agent runs, with the yes already given by typing
the command.  It says what happened in the words every other door uses, and
when the install cannot finish it prints the installer's own line and last
output, so the person has the exact thing that failed.
"""

from __future__ import annotations

import sys

import click

from kiln.cli.output import format_response


@click.command("install-camera")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def install_camera(json_mode: bool) -> None:
    """Add the free add-on Kiln needs to show some printers' cameras.

    Some cameras send video rather than pictures, and Kiln needs one extra
    program to show them.  This adds it to Kiln's own folder.  It is safe
    while a print runs, and nothing needs restarting afterwards.
    """
    from kiln import camera_addon

    if not json_mode and not camera_addon.health()["installed"]:
        click.echo(f"  {camera_addon.WORDS['adding']}")
    result = camera_addon.install(confirm=True)
    ok = bool(result.get("ok"))

    if json_mode:
        status = str(result.get("status", "failed"))
        error = None if ok else {
            "code": f"CAMERA_ADDON_{status.upper()}",
            "message": str(result.get("message", "")),
        }
        click.echo(format_response("success" if ok else "error", data=result, error=error, json_mode=True))
    elif result.get("status") == "failed":
        click.echo(click.style("  Kiln couldn't add the camera add-on here.", fg="yellow"))
        if result.get("installer"):
            click.echo(f"  $ {result['installer']}")
        for line in str(result.get("detail", "")).splitlines():
            click.echo(f"    {line[:200]}")
    else:
        click.echo(click.style(f"  {result.get('message', '')}", fg="green" if ok else "yellow"))

    if not ok:
        sys.exit(1)


def register_install_camera_cli(cli_group: click.Group) -> None:
    """Attach ``kiln install-camera`` to the CLI group."""
    cli_group.add_command(install_camera)
