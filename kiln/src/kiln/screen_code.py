"""A code on the screen: a person's yes, typed into a chat, proven by a
secret only someone looking at this machine's screen could know.

Why this exists.  Many hosts cannot draw the approval dialog (they never
declare MCP elicitation), and "go to a terminal" is not an answer for a
person who lives in a chat.  So when the dialog is not available Kiln
shows a short code in an operating-system notification on the machine it
runs on — the machine the printer is attached to — and the person types
that code in the chat.  The agent relays the words through one tool
(``give_print_code``); Kiln checks them here.  Kiln never reads the chat
and never has to know which app the person is in.

What it proves.  The code is random, held only in this process's memory,
never written to disk, never placed in any tool result, log line or audit
row until it has been answered.  Nothing an agent can call reveals it.  A
relayed code therefore proves that someone who could see this screen
chose to type it — the same rung as a person at a terminal (grade B).  It
does not prove more than that: an agent that can take screenshots of this
machine could read the banner while it is live.  The limits are in the
open, and the refusal that offers the code says where else a yes can come
from.

One vocabulary.  The answer carries the dialog's three yeses in the same
words: ``4821`` is this print, ``4821 2h`` and ``4821 today`` also open a
standing window on the one printer the print was aimed at, and a typed
length (``4821 45m``) is a yes for that long — parsed by the dialog's own
parser, so a code cannot ask for anything the dialog could not.  Every
printer at once is not offered here; that is a terminal's or the account
page's grant.

Limits that make guessing pointless.  A code lives ten minutes and is
spent by one answer; three wrong guesses void every live code and start a
minute's cool-down; a printer gets at most a handful of codes an hour; and
an answer that lands before a person could have read the banner is
refused without counting, and the banner shown again.
"""

from __future__ import annotations

import json
import logging
import os
import plistlib
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Digits in a code.  Four is enough once guessing is bounded below.
CODE_DIGITS = 4
#: How long a shown code can be answered.
CODE_TTL_S = 600.0
#: Wrong guesses before every live code is voided.
MAX_WRONG_TRIES = 3
#: The wait after a void, and after too many codes in an hour.
COOLDOWN_S = 60.0
#: An answer sooner than this after the banner cannot have been read by a
#: person; it is refused without counting and the banner shown again.
MIN_READ_S = 2.0
#: Codes a printer may be shown in an hour.
MAX_ISSUES_PER_HOUR = 5
#: The same live code is shown again at most this often.
RESHOW_EVERY_S = 30.0
#: The answer's second word, in the dialog's terms.
_WORD_TO_CHOICE = {"2h": "next_two_hours", "today": "rest_of_today"}
_EVERY_PRINTER_WORDS = {"all", "every", "everywhere", "fleet"}

#: Outcomes of :func:`issue`.
SHOWN = "shown"
COOLDOWN = "cooldown"
UNAVAILABLE = "unavailable"
#: Outcomes of :func:`answer`.
ANSWERED = "answered"
WRONG = "wrong"
VOIDED = "voided"
TOO_FAST = "too_fast"
NOT_A_CODE = "not_a_code"
EVERY_PRINTER_NOT_HERE = "every_printer_not_here"


@dataclass
class Issued:
    """One code, shown for one print on one printer."""

    code: str
    tool: str
    file_name: str
    file_sha256: str
    printer_name: str
    host: str
    issued_at: float
    issued_mono: float
    expires_at: float
    shown_at: float
    wrong_tries: int = 0
    #: How a person calls the printer on the banner; matching uses
    #: ``printer_name``, never this.
    printer_label: str = ""

    def live(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) < self.expires_at


@dataclass
class Answered:
    """A code the person typed, waiting to be spent by the start it covers."""

    issued: Issued
    words: str
    answered_at: float
    choice: str
    typed_duration: str
    #: The parsed answer (a ``kiln.print_consent.DialogAnswer``).
    answer: Any


_lock = threading.Lock()
_live: dict[tuple[str, str], Issued] = {}
_answered: dict[tuple[str, str], Answered] = {}
_cooldown_until: dict[str, float] = {}
_issued_times: dict[str, list[float]] = {}
#: Tests swap the notifier; production shows a real banner.
_show_hook: Callable[[Issued], bool] | None = None


def _reset_for_tests() -> None:
    """Forget every code.  The notifier hook is left as the test set it:
    a test that forgets to install one would otherwise put a real banner
    on the developer's screen."""
    global _last_kiln_status  # noqa: PLW0603
    with _lock:
        _live.clear()
        _answered.clear()
        _cooldown_until.clear()
        _issued_times.clear()
    _last_kiln_status = None


def _key(file_name: str, printer_name: str | None) -> tuple[str, str]:
    name = os.path.basename(str(file_name or "").strip()).lower()
    return name, str(printer_name or "").strip().lower()


def _printer_key(printer_name: str | None) -> str:
    return str(printer_name or "").strip().lower()


# ---------------------------------------------------------------------------
# The screen
# ---------------------------------------------------------------------------


def screen_available() -> bool:
    """Whether this machine can put a banner in front of a person.  A
    guess from the platform and the session; :func:`_show` is the truth.
    ``KILN_SCREEN_CODE=0`` turns the door off (a headless box, a person
    who wants no banners; the test suite, so no test ever shows one)."""
    if os.environ.get("KILN_SCREEN_CODE", "").strip().lower() in ("0", "false", "off", "no"):
        return False
    if sys.platform == "darwin" or sys.platform.startswith("win"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


#: What a file is called on the banner: its name, without the print-file
#: suffix a person never typed.  Longest first, so ``x.gcode.3mf`` loses both.
_SHOWN_WITHOUT = (".gcode.3mf", ".3mf", ".gcode", ".stl", ".obj", ".step", ".stp")


def _shown_name(file_name: str) -> str:
    name = os.path.basename(str(file_name or ""))
    for suffix in _SHOWN_WITHOUT:
        if name.lower().endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


def banner_text(issued: Issued) -> tuple[str, str, str]:
    """``(title, subtitle, message)`` — the words on the banner.  The one
    place the code is put into words.

    The code goes in the TITLE: a banner cuts its body off after a line
    or two, and the code is the one thing the person came to read.  The
    printer is named the way the person names it, never by Kiln's
    ``default`` alias."""
    printer = issued.printer_label or issued.printer_name or "your printer"
    return (
        f"Kiln print code {issued.code}",
        f"{_shown_name(issued.file_name)} on {printer}",
        "Type the code in your chat to print it. "
        "Add 2h or today to keep printing without asking.",
    )


def _applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


#: The name Windows files Kiln's pop-ups under.  Registered for the current
#: user (no administrator, no prompt) with Kiln's name and icon, so a toast
#: reads "Kiln" — not "Windows PowerShell", and not silently dropped, which
#: is what an unregistered name gets on current Windows.
_WINDOWS_APP_ID = "Kiln3D.Kiln"
_PACKAGED_PNG = Path(__file__).parent / "data" / "notifier" / "Kiln.png"
#: Every character PowerShell reads as a single quote.  Inside a
#: single-quoted string each is escaped by doubling it; missing one is a
#: way out of the string.
_PS_QUOTES = "'\u2018\u2019\u201a\u201b"


def _ps_literal(text: str) -> str:
    """A PowerShell single-quoted string: nothing inside it is evaluated —
    no ``$(...)``, no variables — whatever a file's name holds."""
    out = str(text)
    for quote in _PS_QUOTES:
        out = out.replace(quote, quote + quote)
    return "'" + out + "'"


def _windows_icon() -> str:
    """Kiln's icon at a stable path Windows can read, or ``""``."""
    try:
        if not _PACKAGED_PNG.is_file():
            return ""
        home = _kiln_home() / "notifier"
        home.mkdir(parents=True, exist_ok=True)
        dest = home / "Kiln.png"
        if not dest.is_file() or dest.stat().st_size != _PACKAGED_PNG.stat().st_size:
            shutil.copyfile(_PACKAGED_PNG, dest)
        return str(dest)
    except Exception:  # noqa: BLE001 — no icon is a plainer toast, not an error
        return ""


def _windows_toast_script(issued: Issued) -> str:
    """One line of Windows PowerShell 5.1: register Kiln's name and icon
    for this user, then show the toast.  Read from standard input, so the
    code never sits on a command line; every word from outside travels
    inside a single-quoted literal and XML-escaped, so a file's name cannot
    become a command."""
    from xml.sax.saxutils import escape

    title, subtitle, message = banner_text(issued)
    toast = (
        '<toast><visual><binding template="ToastGeneric">'
        f"<text>{escape(title)}</text><text>{escape(subtitle)}</text><text>{escape(message)}</text>"
        "</binding></visual></toast>"
    )
    icon = _windows_icon()
    steps = [
        f"$id={_ps_literal(_WINDOWS_APP_ID)}",
        "$k='HKCU:\\Software\\Classes\\AppUserModelId\\'+$id",
        "if(-not(Test-Path $k)){New-Item -Path $k -Force|Out-Null}",
        "New-ItemProperty -Path $k -Name DisplayName -Value 'Kiln' -PropertyType String -Force|Out-Null",
    ]
    if icon:
        steps.append(f"New-ItemProperty -Path $k -Name IconUri -Value {_ps_literal(icon)} -PropertyType String -Force|Out-Null")
    steps += [
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]|Out-Null",
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]|Out-Null",
        "$x=New-Object Windows.Data.Xml.Dom.XmlDocument",
        f"$x.LoadXml({_ps_literal(toast)})",
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id).Show([Windows.UI.Notifications.ToastNotification]::new($x))",
    ]
    # One line, ended: PowerShell reading a script from standard input runs
    # each line as it completes.
    return ";".join(steps) + "\n"


def _show_command(issued: Issued) -> tuple[list[str], str | None] | None:
    """``(argv, stdin)`` that puts the banner up on this platform, or ``None``.

    The words — the code among them — travel on the notifier's STANDARD
    INPUT wherever the notifier can read one.  Any program on the machine
    can list every process's arguments, so a code passed as an argument is
    readable by exactly the agent it exists to keep out.  Linux's
    ``notify-send`` takes its text only as arguments; there the code sits
    on its command line for the moment it runs, and that is said here
    rather than hidden."""
    title, subtitle, message = banner_text(issued)
    if sys.platform == "darwin":
        script = (
            f"display notification {_applescript_string(message)} "
            f"with title {_applescript_string(title)} subtitle {_applescript_string(subtitle)}"
        )
        return ["osascript", "-"], script
    if sys.platform.startswith("win"):
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", "-"], _windows_toast_script(issued)
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return ["notify-send", "-a", "Kiln", title, f"{subtitle}\n{message}"], None
    return None


# ---------------------------------------------------------------------------
# The Mac's own notifier: Kiln.app
# ---------------------------------------------------------------------------
#
# macOS lets an app post a notification only after the person allows it, and
# only a native app can ask.  A banner sent through the system script runner
# arrives labelled "Script Editor" with its icon.  So on a Mac, Kiln ships a
# small helper app (built by kiln/scripts/build_notifier.py) that posts as Kiln,
# with Kiln's icon, and can say honestly whether it is allowed to.  It is
# copied out of the package into Kiln's home once, so it runs from a place
# that is Kiln's own and survives a reinstall of the package.

_PACKAGED_APP = Path(__file__).parent / "data" / "notifier" / "Kiln.app"
_NOTIFIER_EXE = Path("Contents") / "MacOS" / "kiln-notifier"
#: The helper's answers to ``status``.
KILN_ALLOWED = "authorized"
KILN_OFF = "denied"
KILN_NOT_ASKED = "not_determined"
#: How long the first-time ask may hold a banner before the old route shows
#: the code; the ask itself keeps running and is answered whenever the
#: person gets to it.
_ASK_WAIT_S = 8.0


def _kiln_home() -> Path:
    return Path(os.environ.get("KILN_HOME") or Path.home() / ".kiln")


def _bundle_version(app: Path) -> str:
    try:
        with open(app / "Contents" / "Info.plist", "rb") as fh:
            return str(plistlib.load(fh).get("CFBundleVersion") or "")
    except Exception:  # noqa: BLE001 — unreadable is "no version"
        return ""


def _notifier_path() -> Path | None:
    """The installed helper's executable, installing or updating it from
    the package first; ``None`` when this is not a Mac or the package
    carries no helper.  Never raises."""
    if sys.platform != "darwin" or not (_PACKAGED_APP / _NOTIFIER_EXE).is_file():
        return None
    installed = _kiln_home() / "notifier" / "Kiln.app"
    try:
        if _bundle_version(installed) != _bundle_version(_PACKAGED_APP) or not (installed / _NOTIFIER_EXE).is_file():
            if installed.exists():
                shutil.rmtree(installed)
            installed.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(_PACKAGED_APP, installed, symlinks=True)
        exe = installed / _NOTIFIER_EXE
        exe.chmod(0o755)
        return exe
    except Exception as exc:  # noqa: BLE001 — no helper is the old route, not an error
        logger.debug("Kiln notifier not installed: %s", exc)
        return None


def kiln_notifications() -> str | None:
    """Whether macOS lets Kiln post as itself: ``authorized``, ``denied``,
    ``not_determined`` — or ``None`` off a Mac or with no helper.  Asks
    nobody; for ``kiln doctor`` and the banner's own choice of route."""
    exe = _notifier_path()
    if exe is None:
        return None
    try:
        done = subprocess.run([str(exe), "status"], capture_output=True, text=True, timeout=5, check=False)
    except Exception:  # noqa: BLE001
        return None
    answer = str(getattr(done, "stdout", "") or "").strip()
    return answer if answer in (KILN_ALLOWED, KILN_OFF, KILN_NOT_ASKED) else None


#: What the last banner learned about Kiln's own notifications on this Mac,
#: so the refusal can name the switch when they are off.  ``None`` until a
#: banner has been tried, and off a Mac.
_last_kiln_status: str | None = None


def last_kiln_status() -> str | None:
    return _last_kiln_status


def _post_as_kiln(exe: Path, issued: Issued) -> bool:
    title, subtitle, message = banner_text(issued)
    words = json.dumps({"title": title, "subtitle": subtitle, "body": message})
    try:
        done = subprocess.run([str(exe), "post"], input=words, capture_output=True, text=True, timeout=10, check=False)
    except Exception:  # noqa: BLE001
        return False
    return done.returncode == 0


def _show_as_kiln(issued: Issued) -> bool:
    """Post the banner as Kiln when macOS allows it.  The first time, ask
    — the system's own "Kiln Notifications" prompt — and wait a moment for
    the answer; a person who has not answered yet still gets this code by
    the old route, and the ask carries on without us.  False sends the
    caller to the old route."""
    global _last_kiln_status  # noqa: PLW0603
    exe = _notifier_path()
    if exe is None:
        return False
    status = kiln_notifications()
    if status == KILN_NOT_ASKED:
        try:
            ask = subprocess.Popen([str(exe), "request"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            out, _ = ask.communicate(timeout=_ASK_WAIT_S)
            status = str(out or "").strip()
        except subprocess.TimeoutExpired:
            _last_kiln_status = KILN_NOT_ASKED
            return False  # still waiting on the person; the ask stays up
        except Exception:  # noqa: BLE001
            return False
    _last_kiln_status = status
    return status == KILN_ALLOWED and _post_as_kiln(exe, issued)


def _show(issued: Issued) -> bool:
    """Put the banner up.  True only when the platform reported success;
    never raises.  On a Mac, as Kiln when macOS allows it (the helper
    above), else by the system script runner; elsewhere, the platform's own
    notifier."""
    if _show_hook is not None:
        try:
            return bool(_show_hook(issued))
        except Exception:  # noqa: BLE001 — a notifier that fails has not shown
            return False
    if sys.platform == "darwin" and _show_as_kiln(issued):
        return True
    command = _show_command(issued)
    if command is None:
        return False
    argv, stdin = command
    # On Windows a console program started from a windowless one opens a
    # console window of its own; the person would see a black box flash up.
    quiet = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)} if sys.platform.startswith("win") else {}
    try:
        done = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=8, check=False, **quiet)
    except Exception as exc:  # noqa: BLE001 — no binary, a timeout, a dead session
        logger.debug("screen code banner not shown: %s", type(exc).__name__)
        return False
    if done.returncode != 0:
        logger.debug("screen code banner not shown: exit %s", done.returncode)
        return False
    return True


# ---------------------------------------------------------------------------
# Issue
# ---------------------------------------------------------------------------


def _new_code() -> str:
    taken = {i.code for i in _live.values()}
    for _ in range(50):
        code = "".join(secrets.choice("0123456789") for _ in range(CODE_DIGITS))
        if code not in taken:
            return code
    raise RuntimeError("could not pick an unused code")


def _prune(now: float) -> None:
    for key in [k for k, i in _live.items() if not i.live(now)]:
        del _live[key]
    for key in [k for k, a in _answered.items() if now - a.answered_at >= CODE_TTL_S]:
        del _answered[key]


def issue(
    *, tool: str, file_name: str, file_sha256: str, printer_name: str | None, host: str = "",
    printer_label: str = "",
) -> dict[str, Any]:
    """Show a code for this print, or say why not.  The result NEVER
    carries the code.  Outcomes: ``shown`` (``again`` when it was already
    live), ``cooldown`` (``seconds_left``), ``unavailable`` (``reason``)."""
    if not screen_available():
        return {"outcome": UNAVAILABLE, "reason": "no_screen"}
    now = time.time()
    key = _key(file_name, printer_name)
    pkey = _printer_key(printer_name)
    with _lock:
        _prune(now)
        until = _cooldown_until.get(pkey, 0.0)
        if until > now:
            return {"outcome": COOLDOWN, "seconds_left": int(until - now) + 1}
        current = _live.get(key)
        if current is not None:
            again = now - current.shown_at >= RESHOW_EVERY_S
            if again:
                if _show(current):
                    current.shown_at = now
                else:
                    return {"outcome": UNAVAILABLE, "reason": "banner_failed"}
            return {"outcome": SHOWN, "again": True, "reshown": again, "expires_in": int(current.expires_at - now)}
        times = [t for t in _issued_times.get(pkey, []) if now - t < 3600.0]
        if len(times) >= MAX_ISSUES_PER_HOUR:
            _cooldown_until[pkey] = now + COOLDOWN_S
            _issued_times[pkey] = times
            return {"outcome": COOLDOWN, "seconds_left": int(COOLDOWN_S)}
        issued = Issued(
            code=_new_code(), tool=tool, file_name=os.path.basename(file_name) or file_name,
            file_sha256=file_sha256 or "", printer_name=str(printer_name or ""), host=host,
            issued_at=now, issued_mono=time.monotonic(), expires_at=now + CODE_TTL_S, shown_at=now,
            printer_label=str(printer_label or ""),
        )
        if not _show(issued):
            return {"outcome": UNAVAILABLE, "reason": "banner_failed"}
        _live[key] = issued
        times.append(now)
        _issued_times[pkey] = times
    logger.info("screen code shown for %s on %s (%s)", issued.file_name, issued.printer_name or "default", tool)
    return {"outcome": SHOWN, "again": False, "reshown": False, "expires_in": int(CODE_TTL_S)}


# ---------------------------------------------------------------------------
# Answer
# ---------------------------------------------------------------------------


def parse_words(words: str) -> tuple[str, str, str] | None:
    """``(code, choice, typed_duration)`` from what the person typed, or
    ``None`` when the words are not a code.  Raises ``ValueError`` when
    the words ask for every printer."""
    parts = str(words or "").strip().split()
    if not parts or not re.fullmatch(rf"\d{{{CODE_DIGITS}}}", parts[0]):
        return None
    rest = [p.lower() for p in parts[1:]]
    if any(p in _EVERY_PRINTER_WORDS for p in rest):
        raise ValueError(EVERY_PRINTER_NOT_HERE)
    if not rest:
        return parts[0], "this_print", ""
    if len(rest) == 1 and rest[0] in _WORD_TO_CHOICE:
        return parts[0], _WORD_TO_CHOICE[rest[0]], ""
    return parts[0], "this_print", " ".join(rest)


def answer(words: str, *, host: str = "") -> dict[str, Any]:
    """Judge the words the person typed.  Outcomes: ``answered`` (with the
    ``Answered`` record), ``wrong`` (``tries_left``), ``voided``
    (``seconds_left``), ``too_fast``, ``not_a_code``,
    ``every_printer_not_here``.  Wrong guesses count against every live
    code, because a guess names none of them."""
    try:
        parsed = parse_words(words)
    except ValueError:
        return {"outcome": EVERY_PRINTER_NOT_HERE}
    if parsed is None:
        return {"outcome": NOT_A_CODE}
    code, choice, typed = parsed
    now = time.time()
    with _lock:
        _prune(now)
        match = next(((k, i) for k, i in _live.items() if i.code == code), None)
        if match is None:
            if not _live:
                return {"outcome": WRONG, "tries_left": 0, "nothing_live": True}
            worst = 0
            for issued in _live.values():
                issued.wrong_tries += 1
                worst = max(worst, issued.wrong_tries)
            if worst >= MAX_WRONG_TRIES:
                for issued in _live.values():
                    _cooldown_until[_printer_key(issued.printer_name)] = now + COOLDOWN_S
                _live.clear()
                logger.warning("screen codes voided after %d wrong guesses", worst)
                return {"outcome": VOIDED, "seconds_left": int(COOLDOWN_S)}
            return {"outcome": WRONG, "tries_left": MAX_WRONG_TRIES - worst}
        key, issued = match
        if time.monotonic() - issued.issued_mono < MIN_READ_S:
            issued.shown_at = 0.0  # so the next issue shows the banner again
            return {"outcome": TOO_FAST}
        from kiln.print_consent import FIELD_ANSWER, FIELD_FOR_HOW_LONG, answer_from_content

        parsed_answer = answer_from_content(
            "accept", {FIELD_ANSWER: choice, FIELD_FOR_HOW_LONG: typed}, offer_window=True, offer_fleet=False,
        )
        if not parsed_answer.accepted:
            return {"outcome": NOT_A_CODE, "detail": parsed_answer.detail}
        del _live[key]
        record = Answered(
            issued=issued, words=str(words).strip(), answered_at=now,
            choice=choice, typed_duration=typed, answer=parsed_answer,
        )
        _answered[key] = record
    logger.info("screen code answered for %s on %s", issued.file_name, issued.printer_name or "default")
    return {"outcome": ANSWERED, "record": record, "host": host}


def take_answer(file_name: str, printer_name: str | None) -> Answered | None:
    """The unspent answer for this print on this printer, spent now."""
    now = time.time()
    with _lock:
        _prune(now)
        return _answered.pop(_key(file_name, printer_name), None)


def status() -> dict[str, Any]:
    """What is live, without the codes — for doctor and status lines."""
    now = time.time()
    with _lock:
        _prune(now)
        return {
            "screen": screen_available(),
            "live": [
                {"file": i.file_name, "printer": i.printer_name, "expires_in": int(i.expires_at - now)}
                for i in _live.values()
            ],
            "answered_waiting": [
                {"file": a.issued.file_name, "printer": a.issued.printer_name, "choice": a.choice}
                for a in _answered.values()
            ],
            "cooldown": {p: int(t - now) for p, t in _cooldown_until.items() if t > now},
        }


# ---------------------------------------------------------------------------
# A host whose own hooks answer dialogs
# ---------------------------------------------------------------------------

#: Where Claude Code keeps hooks.  An ``Elicitation`` hook there can answer
#: an MCP dialog on the person's behalf before they see it (the host's own
#: documented feature), so a dialog on such a host proves nothing; Kiln
#: skips it and shows the code instead.
_HOOK_SETTINGS = ("~/.claude/settings.json", ".claude/settings.json", ".claude/settings.local.json")


def _hook_matches(entry: Any, server_name: str) -> bool:
    matcher = str((entry or {}).get("matcher") or "").strip() if isinstance(entry, dict) else ""
    if matcher in ("", "*"):
        return True
    try:
        return re.search(matcher, server_name, re.IGNORECASE) is not None or matcher.lower() in server_name.lower()
    except re.error:
        return matcher.lower() in server_name.lower()


def host_dialog_hook(client_name: str | None, *, server_name: str = "kiln", cwd: str | None = None) -> str | None:
    """The settings file whose ``Elicitation`` hook would answer this
    server's dialogs on the host that is connected, or ``None``.  Only
    Claude Code is known to run such hooks (as ``claude-code``, or as the
    desktop app's Code tab, ``local-agent-mode-*``); other hosts are not
    read."""
    name = str(client_name or "").lower()
    # Claude Code, in a terminal or in the desktop app's Code tab — which
    # runs it under its own client name and reads the same settings files.
    if "claude" not in name and "local-agent-mode" not in name:
        return None
    base = Path(cwd or os.getcwd())
    for raw in _HOOK_SETTINGS:
        path = Path(raw).expanduser() if raw.startswith("~") else base / raw
        try:
            data = json.loads(path.read_text())
        except Exception:  # noqa: BLE001 — absent or unreadable is no hook
            continue
        hooks = (data.get("hooks") or {}) if isinstance(data, dict) else {}
        entries = hooks.get("Elicitation") or []
        if any(_hook_matches(e, server_name) for e in entries):
            return str(path)
    return None
