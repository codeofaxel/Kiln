"""A person at a terminal, shown the print and asked — the CLI's no-token path.

``cli_gate`` takes a preview token the way the tools do.  A person typing
``kiln print part.3mf`` at their own terminal has no token and no agent to
fetch one; what they have is the ability to look and answer.  This is that
path: render the print through the best door a terminal can open (a viewer
link, else the renders), open it, ask on the terminal, and on a yes grant
the same clearance a token grants, with the door it came through.

It is deliberately not a flag.  A flag anyone can type is not consent: an
agent that wants to skip the question only has to add it.  What is checked
is that a person is present — stdin AND stdout are terminals — so ``yes |
kiln print`` and an agent's subprocess get the token refusal, not a
question nobody will read.

A file that lives only on the printer, or one Kiln cannot draw, is
described and asked about; the clearance then records ``described`` as its
door.  What this never does is claim a picture it did not draw.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import click

from kiln.print_consent import SOURCE_TERMINAL, PrintConsent, set_consent

logger = logging.getLogger(__name__)

#: Formats the visualiser can draw.  Everything else is described, not shown.
_RENDERABLE = (".stl", ".3mf", ".obj", ".scad")


def _person_is_present() -> bool:
    """A terminal with a person at both ends of it."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:  # noqa: BLE001 — a closed stream is not a person
        return False


def _audit(tool: str, action: str, details: dict[str, Any]) -> None:
    """Same audit table the tools write to, so the trail reads as one."""
    try:
        from kiln.persistence import get_db

        get_db().log_audit(tool_name=tool, safety_level="confirm", action=action, details=details)
    except Exception:  # noqa: BLE001 — bookkeeping never blocks or starts a print
        logger.debug("audit write failed for %s/%s", tool, action)


def render_for_terminal(file_path: str) -> tuple[list[str], str | None]:
    """``(image paths, viewer link)`` for a local mesh; ``([], None)`` otherwise.

    The renderer records what it drew (``kiln.preview_evidence``), and the
    link door records the link or why it could not — that record is what
    the token judge reads.  Never raises.
    """
    if not file_path or not os.path.isfile(file_path):
        return [], None
    if not file_path.lower().endswith(_RENDERABLE):
        return [], None
    try:
        from kiln.model_visualizer import visualize_model

        result = visualize_model(file_path, share_link=True)
        if not result.get("success"):
            return [], None
        images = [v["path"] for v in result.get("views", []) if v.get("path")]
        return images, result.get("viewer_url") or None
    except Exception as exc:  # noqa: BLE001 — described, not shown
        logger.debug("preview render failed for %s: %s", file_path, exc)
        return [], None


def confirm_print_at_terminal(
    *,
    tool: str,
    file_path: str,
    printer_name: str | None = None,
    json_mode: bool = False,
) -> bool:
    """Ask the person at this terminal.  ``True`` on a yes (clearance granted),
    ``False`` when nobody is here to ask.  Exits the command on a no.
    """
    if not _person_is_present():
        return False

    from kiln import print_signoff

    name = os.path.basename(file_path) or file_path
    where = f" on {printer_name}" if printer_name else ""
    images, viewer_url = render_for_terminal(file_path)
    if images or viewer_url:
        click.echo(f"Preview of {name}:")
        for path in images:
            click.echo(f"  {path}")
        if viewer_url:
            click.echo(f"  viewer: {viewer_url}")
        try:
            click.launch(viewer_url or images[0])
        except Exception as exc:  # noqa: BLE001 — the path is printed either way
            logger.debug("could not open preview: %s", exc)
        question = f"You have seen the preview. Start printing {name}{where}?"
    else:
        click.echo(
            f"No preview could be rendered for {name} — Kiln is describing this "
            "job, not showing it. Approve only if you know what this file is."
        )
        question = f"Start printing {name}{where}?"

    if not click.confirm(question, default=False):
        _audit(tool, "consent_refused", {"file": file_path, "action": "decline"})
        click.echo("Nothing was sent to the printer.")
        sys.exit(1)

    # The yes is a person's; the DOOR is still judged.  A rendered file gets
    # a token through the same judge the tools face (a link beats a PNG;
    # a PNG is accepted only when the link door said why it could not), so
    # the clearance records which door the person actually looked through.
    door = "described"
    if images or viewer_url:
        from kiln.server import issue_preview_token

        issued = issue_preview_token(file_path, door="url" if viewer_url else "png")
        if not issued.get("success"):
            refusal = issued.get("error") if isinstance(issued.get("error"), dict) else {}
            click.echo(
                click.style(
                    "The preview shown does not meet the sign-off rule: "
                    + str(refusal.get("message") or issued),
                    fg="red",
                )
            )
            sys.exit(1)
        verdict = print_signoff.token_verdict(
            tool, file_path, issued["token"], printer_name=printer_name,
        )
        if not verdict.ok:
            click.echo(click.style(verdict.message, fg="red"))
            sys.exit(1)
        door = verdict.door or issued.get("door") or door
    else:
        print_signoff.grant(tool, file_path, printer_name, source=SOURCE_TERMINAL, door=door)

    # Recorded where the tools' gate reads first, so a command that goes on
    # to call a gated tool (queue submit -> submit_job) is not asked twice.
    set_consent(
        PrintConsent(tool=tool, file_name=file_path, printer_name=printer_name, source=SOURCE_TERMINAL)
    )
    _audit(
        tool,
        "consent_granted",
        {"file": file_path, "by": "user", "consent": SOURCE_TERMINAL, "door": door},
    )
    return True
