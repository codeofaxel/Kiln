"""Ask a person for what their printer needs to connect.

One helper for every terminal setup flow (``kiln setup``, the bridge's
first-printer offer), reading the one list of what each kind of printer
needs in :mod:`kiln.printer_backends`.  Before it, each flow asked in its
own words and they disagreed: one asked for a value discovery had already
read off the printer, and two named a different credential for the same
printer.
"""

from __future__ import annotations

from collections.abc import Mapping

import click

from kiln.printer_backends import backend_for, needs_to_ask


def _sentence_case(text: str) -> str:
    return text[:1].upper() + text[1:]


def ask_connection_needs(
    printer_type: str,
    *,
    found: Mapping[str, object] | None = None,
    discovered: bool,
) -> dict[str, str | None]:
    """Prompt for what *printer_type* still needs; return it by config key.

    *found* is what discovery read off the printer, by config key: it is
    said back to the person rather than asked for, and returned with the
    answers.  A required need is asked until it is answered; an optional
    one is asked only for a printer whose address was typed by hand (see
    :func:`~kiln.printer_backends.needs_to_ask`), and Enter skips it.
    """
    found = found or {}
    answers: dict[str, str | None] = {}
    backend = backend_for(printer_type)
    for step in backend.first if backend else ():
        click.echo(f"  First: {step}")
    for need in backend.needs if backend else ():
        value = str(found.get(need.key) or "").strip()
        if value:
            click.echo(f"  {_sentence_case(need.name)}: {value} (read from the printer)")
            answers[need.key] = value
    for need in needs_to_ask(printer_type, found=found, discovered=discovered):
        label = f"  {_sentence_case(need.name)} ({need.where})"
        if need.required:
            answer = click.prompt(label)
        else:
            answer = click.prompt(f"{label}, or press Enter to skip", default="", show_default=False)
        answers[need.key] = str(answer).strip() or None
    # Whatever else discovery read (an Elegoo's mainboard ID) rides along
    # unasked: nobody has to supply it, and the printer is reached faster.
    for key, value in found.items():
        if key != "host" and key not in answers and str(value or "").strip():
            answers[key] = str(value).strip()
    return answers
