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

from kiln.cli.output import format_error
from kiln.print_consent import (
    SOURCE_CI_BYPASS,
    SOURCE_STANDING_OPT_IN,
    SOURCE_TERMINAL,
    PrintConsent,
    set_consent,
)

logger = logging.getLogger(__name__)

#: Formats the visualiser can draw.  Everything else is described, not shown.
_RENDERABLE = (".stl", ".3mf", ".obj", ".scad")


def _person_is_present() -> bool:
    """A terminal with a person at both ends of it — the same test the
    standing-window command uses, so "a person" means one thing."""
    from kiln.consent_windows import person_at_terminal

    return person_at_terminal()


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
    preview_token: str | None = None,
) -> bool:
    """Ask the person at this terminal.  ``True`` on a yes (clearance granted),
    ``False`` when nobody is here to ask.  Exits the command on a no.

    The yes is the person's — the SAID GO half.  The SAW half is still
    judged: a rendered file gets a token through the same judge the tools
    face (a link beats a PNG; a PNG is accepted only when the link door
    said why it could not); a token handed in on the command line is the
    saw half already and is not re-rendered; a file nothing can draw is
    described, the person is told so, and the record says ``described``.
    Both halves then go through the one gate every door uses.
    """
    if not _person_is_present():
        return False

    from kiln import print_signoff
    from kiln.consent_windows import local_identity

    name = os.path.basename(file_path) or file_path
    where = f" on {printer_name}" if printer_name else ""
    images: list[str] = []
    viewer_url: str | None = None
    if preview_token:
        click.echo(f"A preview token for {name} is on record; the print it was issued for is what starts.")
        question = f"Start printing {name}{where}?"
    else:
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

    door = ""
    token = preview_token
    if not token:
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
            token = issued["token"]
        else:
            door = "described"

    # Recorded where the tools' gate reads, so a command that goes on to
    # call a gated tool (queue submit -> submit_job) is not asked twice.
    # The yes is for this one print on the printer it is aimed at.
    set_consent(
        PrintConsent(
            tool=tool, file_name=file_path, printer_name=printer_name,
            source=SOURCE_TERMINAL, door=door, identity=local_identity(),
        )
    )
    from kiln.server import _preview_gate_error

    block = _preview_gate_error(tool, file_path, token, printer_name=printer_name)
    if block is not None:
        refusal = block.get("error") if isinstance(block.get("error"), dict) else {}
        click.echo(click.style(str(refusal.get("message") or block), fg="red"))
        sys.exit(1)
    cleared = print_signoff.current()
    _audit(
        tool,
        "consent_granted",
        {
            "file": file_path, "by": "user", "consent": SOURCE_TERMINAL,
            "door": (cleared.door if cleared else door) or door, "identity": local_identity(),
        },
    )
    return True


def _ci_bypass_set() -> bool:
    return os.environ.get("KILN_SKIP_PREVIEW_GATE", "").strip().lower() in ("1", "true", "yes")


def confirm_standing_auto_print_at_terminal(
    *,
    tool: str,
    scope: str,
    json_mode: bool = False,
) -> None:
    """One yes, given in person, that covers every print an unattended mode
    will start.  Returns on a yes; exits the command otherwise.

    A watched folder cannot show each file to anyone — that is what it is
    for.  The flag that arms it is not the consent: an agent driving a shell
    can type a flag, drop a file, and print unseen, which is the hole the
    gate exists to close.  So the person is asked once, before it arms, and every
    start afterwards is audited as resting on that answer.  With nobody at
    the terminal it does not arm; a real unattended service is a CI-style
    decision and takes the same audited switch.
    """
    if _ci_bypass_set():
        _audit(tool, "preview_gate_skipped", {"scope": scope, "consent": SOURCE_CI_BYPASS})
        return
    if not _person_is_present():
        _audit(tool, "preview_gate_refused", {"scope": scope, "reason": "no_person_at_terminal"})
        click.echo(
            format_error(
                f"{tool} will not start prints unattended: {scope}, and nobody is "
                "at this terminal to agree to that. Run it in a terminal and "
                "answer the prompt, or for an unattended service set "
                "KILN_SKIP_PREVIEW_GATE=1 (every start is audited).",
                code="PREVIEW_NOT_CONFIRMED",
                json_mode=json_mode,
            )
        )
        sys.exit(1)
    click.echo(f"{scope}. No preview will be shown for those prints.")
    if not click.confirm("Continue?", default=False):
        _audit(tool, "consent_refused", {"scope": scope, "action": "decline"})
        click.echo("Nothing was started.")
        sys.exit(1)
    _audit(tool, "consent_granted", {"scope": scope, "by": "user", "consent": SOURCE_STANDING_OPT_IN})
