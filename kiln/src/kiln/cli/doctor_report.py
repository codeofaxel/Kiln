"""A bug report written from ``kiln doctor``, for the person to send themselves.

``kiln doctor --bug-report`` runs the usual checkup, then writes what it found
as plain text, puts it on the clipboard and opens Kiln's support page.
Nothing is sent from here: the person reads the report, adds what they were
doing, and sends it from the page.

Before it is copied, the text loses what says where a printer is, how to
unlock it, or whose computer this is: every address, serial and code this
computer holds for its printers (:func:`kiln.outbound_guard.scrub_relayed`),
then secrets, network addresses, links and the account name in home folders
(:func:`kiln.redaction.redact_machine_text`).  A printer's name stays, so the
person can see which machine a line is about.
"""

from __future__ import annotations

import platform
import subprocess
import sys
from collections.abc import Iterable
from typing import Any

from kiln import run_program

#: Kiln's support page, opened on the door for something that broke.
SUPPORT_URL = "https://app.kiln3d.com/support?door=bug"

#: The support page's box takes 7000 characters (web-workshop
#: ``app/support/page.tsx``); the report stays under this so the person's own
#: words still fit beside it.
REPORT_ROOM = 5500

#: The most of any one check's detail a report carries.
LINE_ROOM = 400

_CLIPBOARD_SECONDS = 5


def _mark(check: dict[str, Any]) -> str:
    if check.get("warn"):
        return "warning"
    return "ok" if check.get("ok") else "FAILED"


def _passing(check: dict[str, Any]) -> bool:
    return bool(check.get("ok")) and not check.get("warn")


def _line(check: dict[str, Any]) -> str:
    detail = str(check.get("detail", ""))
    if len(detail) > LINE_ROOM:
        detail = detail[: LINE_ROOM - 1] + "…"
    return f"[{_mark(check)}] {str(check.get('name', '')).replace('_', ' ')}: {detail}"


def clean(text: str) -> str:
    """*text* without this computer's printer addresses, serials and codes,
    secrets, network addresses, links or home-folder account names."""
    from kiln.outbound_guard import scrub_relayed
    from kiln.redaction import redact_machine_text

    return redact_machine_text(scrub_relayed(text))


def build_report(checks: Iterable[dict[str, Any]], *, kiln_version: str) -> str:
    """The checkup as a report: failures first, then warnings, then passing
    checks for as long as they fit the support page's box."""
    head = [
        "Kiln checkup (kiln doctor --bug-report)",
        f"Kiln {kiln_version}, Python {platform.python_version()}, {platform.platform(terse=True)}",
        "",
    ]
    lines: list[str] = []
    used = sum(len(h) + 1 for h in head)
    left_out = 0
    for check in sorted(checks, key=lambda c: (_passing(c), not c.get("warn"))):
        line = _line(check)
        if _passing(check) and used + len(line) + 1 > REPORT_ROOM:
            left_out += 1
            continue
        lines.append(line)
        used += len(line) + 1
    if left_out:
        lines.append(f"({left_out} passing checks left out so the report fits the form.)")
    tail = ["", "What I was doing when it went wrong:", ""]
    return clean("\n".join(head + lines + tail))


def _clipboard_commands() -> tuple[list[str], ...]:
    if sys.platform == "darwin":
        return (["pbcopy"],)
    if sys.platform == "win32":
        return (["clip"],)
    return (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"])


def copy_to_clipboard(text: str) -> bool:
    """Put *text* on the clipboard; whether any clipboard program took it.

    Windows' ``clip`` reads UTF-16 with a byte-order mark, so a printer name
    in another script arrives intact; the others read UTF-8.
    """
    data = text.encode("utf-16" if sys.platform == "win32" else "utf-8")
    for command in _clipboard_commands():
        try:
            done = run_program.run(command, input=data, capture_output=True, timeout=_CLIPBOARD_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if done.returncode == 0:
            return True
    return False


def hand_over(report: str) -> tuple[bool, bool]:
    """Copy *report* and open the support page: ``(copied, opened)``."""
    copied = copy_to_clipboard(report)
    try:
        opened = run_program.open_url(SUPPORT_URL, new=1, autoraise=True)
    except Exception:  # noqa: BLE001 -- the link is printed whether or not a browser took it
        opened = False
    return copied, opened


def words(report: str, *, copied: bool, opened: bool) -> list[str]:
    """What the person is told, in order, after the checkup lines."""
    said = [""]
    if copied:
        said.append("  A bug report is on your clipboard, with network addresses, keys and your user name taken out.")
    else:
        said += ["  Here is a bug report, with network addresses, keys and your user name taken out:", ""]
        said += [f"    {line}" for line in report.splitlines()]
        said.append("")
    if opened:
        said.append('  Kiln\'s support page is open on "Something\'s broken": paste it there, read it, and add what you were doing.')
    else:
        said.append(f'  Open {SUPPORT_URL} and paste it under "Something\'s broken", then add what you were doing.')
    said.append("  Nothing has been sent. You choose what to send.")
    return said
