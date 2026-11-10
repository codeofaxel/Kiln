"""Kiln's camera add-on: finding it, who needs it, what is said, adding it.

Some cameras send a video stream rather than pictures, and Kiln needs one
extra program to cut a picture out of a stream.  This module is the one home
of that program: where it is found, which printers need it, every sentence a
person reads about it, the block an agent reads, and the install.

Nothing here is remembered between calls.  Each lookup reads the computer as
it is now, so an add-on installed a moment ago, by this process or another,
is found on the next call with no restart.
"""

from __future__ import annotations

import contextlib
import importlib
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from kiln.printers.base import CameraStreamError, ExternalCamera

ADDON = "camera"
REFUSAL_CODE = "FFMPEG_REQUIRED"
RESULT_KEY = "camera_addon"

PIP_PACKAGE = "imageio-ffmpeg"
PIP_REQUIREMENT = f"{PIP_PACKAGE}>=0.6,<1"

MOMENTS = ("snapshot", "setup", "health")

# Every sentence a person reads about the add-on.  None names the program,
# a protocol or an installer; ``{command}`` and ``{printer}`` are filled in.
WORDS: Mapping[str, str] = MappingProxyType(
    {
        "needed": "Kiln needs one free add-on to show this printer's camera.",
        "needed_yours": "Kiln needs one free add-on to show your camera.",
        "ask": "Want me to add it now?",
        "setup": "{printer} is ready. Kiln needs one free add-on to show its camera.",
        "cli_line": "Camera: needs Kiln's free camera add-on. Add it with: {command}",
        "tool_line": (
            "Camera: needs Kiln's free camera add-on "
            '(ask first, then upgrade_kiln addon="camera").'
        ),
        "panel": (
            "Kiln needs one free add-on to show this printer's camera. "
            "Ask Kiln to add it."
        ),
        "confirm": (
            "Kiln needs one free add-on to show your printer's camera. "
            "Want me to add it now? It only takes a moment, and nothing needs restarting."
        ),
        "adding": "Adding Kiln's camera add-on...",
        "added": "Camera add-on added. Kiln can show your printer's camera now.",
        "offline": (
            "Kiln couldn't reach the internet to get the camera add-on. "
            "Try again when this computer is online."
        ),
        "already": "The camera add-on is already here. Nothing to add.",
        "failed": (
            "Kiln couldn't add the camera add-on here. "
            "Run this once on this computer: {command}"
        ),
        "not_here": (
            "Kiln needs one free add-on to show this printer's camera. "
            "Ask Kiln on the computer next to your printer to add it."
        ),
        "pictures_only": (
            "Kiln shows this printer's camera as pictures. "
            "Live video isn't available for it."
        ),
        "doctor_needed": (
            "{printer}'s camera needs Kiln's free camera add-on. Add it with: {command}"
        ),
        "doctor_ready": "add-on ready",
    }
)

_MODULE = "imageio_ffmpeg"
_SYSTEM_PLACES = ("/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg")
_CHECK_TIMEOUT_S = 10
# The stream these cameras send is encrypted, so the program must open one.
_SECURE_PROTOCOL = "tls"

_INSTALL_TOOL = {"tool": "upgrade_kiln", "arguments": {"addon": ADDON, "confirm": True}}

_NOTE_FOR_AGENT_ONLY = (
    "For you, not the person: the add-on is a bundled ffmpeg kept in Kiln's own "
    "folder; an ffmpeg already on the computer works too. Never say those names to them."
)
_NOTE_NEEDED = (
    "Say `message`, then ask `ask`. Only after the person says yes, call "
    'upgrade_kiln(addon="camera", confirm=true). It is free, needs no restart and '
    "is fine to add while a print runs. Do not hand them a command unless that call "
    'answers status "failed". After it answers "installed", take the snapshot again '
    f"and show the picture. {_NOTE_FOR_AGENT_ONLY}"
)


# ---------------------------------------------------------------------------
# Finding it
# ---------------------------------------------------------------------------


def addon_dir() -> Path:
    """The folder the add-on lives in once Kiln has added it.  Never created here."""
    from kiln.startup_failure import kiln_home

    return kiln_home() / "addons" / ADDON


def ffmpeg_path() -> str | None:
    """The program that cuts a picture from a stream, or ``None``.

    Looked for, in order: a program already on this computer, the
    ``kiln3d[camera]`` extra in Kiln's environment, then Kiln's own add-on
    folder.  Read fresh on every call.
    """
    found = _resolve()
    return found[0] if found else None


def source() -> str | None:
    """Where :func:`ffmpeg_path` found it: ``"system"``, ``"extra"`` or ``"addon"``."""
    found = _resolve()
    return found[1] if found else None


def health() -> dict[str, Any]:
    """``{"installed", "source"}`` for a health report; callers add who needs it."""
    where = source()
    return {"installed": where is not None, "source": where}


def _resolve() -> tuple[str, str] | None:
    system = _system_program()
    if system:
        return system, "system"
    folder = addon_dir()
    # Only a folder that exists joins the search, and it joins last, so it
    # can never stand in front of anything Kiln's environment provides.
    if folder.is_dir() and str(folder) not in sys.path:
        sys.path.append(str(folder))
    program = _bundled_program()
    if program is None:
        return None
    return program, ("addon" if _is_inside(program, folder) else "extra")


def _system_program() -> str | None:
    path = shutil.which("ffmpeg")
    if path:
        return path
    for candidate in _SYSTEM_PLACES:
        if os.path.isfile(candidate):
            return candidate
    return None


def _bundled_program() -> str | None:
    """The program the bundled package names, from wherever it loads right now."""
    for _attempt in range(2):
        # Python remembers folders that had nothing to load; forget that first.
        importlib.invalidate_caches()
        try:
            program = importlib.import_module(_MODULE).get_ffmpeg_exe()
        except Exception:  # noqa: BLE001 -- absent or broken both mean "not here"
            program = None
        if program and os.path.isfile(str(program)):
            return str(program)
        # A copy loaded from a folder that has since moved answers wrongly
        # for ever; drop it so the next look loads what is there now.
        if not _forget_bundled():
            break
    return None


def _forget_bundled() -> bool:
    names = [n for n in sys.modules if n == _MODULE or n.startswith(f"{_MODULE}.")]
    for name in names:
        sys.modules.pop(name, None)
    return bool(names)


def _is_inside(path: str, folder: Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(folder.resolve())
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Who needs it
# ---------------------------------------------------------------------------


def _adapter_says_so(adapter: Any) -> bool:
    try:
        return getattr(adapter, "camera_needs_addon", lambda: False)() is True
    except Exception:  # noqa: BLE001 -- an adapter that cannot say is not nudged
        return False


def needed_by(adapter: Any) -> bool:
    """Whether *adapter*'s camera needs the add-on and this computer lacks it."""
    return _adapter_says_so(adapter) and ffmpeg_path() is None


def printers_needing(adapters: Mapping[str, Any]) -> list[str]:
    """The names in *adapters* whose camera is waiting on the add-on, in order."""
    waiting = [name for name, adapter in adapters.items() if _adapter_says_so(adapter)]
    return waiting if waiting and ffmpeg_path() is None else []


def _camera_needs(camera: ExternalCamera | None) -> bool:
    from kiln.printers.base import _is_rtsp

    return camera is not None and _is_rtsp(camera.snapshot_url or camera.stream_url)


# ---------------------------------------------------------------------------
# What is said
# ---------------------------------------------------------------------------


def command() -> str:
    """The line a person types on this computer to add the add-on."""
    from kiln.cli_command import kiln_command

    return kiln_command("install-camera")


def refusal(*, yours: bool = False) -> CameraStreamError:
    """The refusal a camera door raises when the add-on is missing.

    Its message is the sentence for the person (``yours`` for a camera they
    registered themselves) and its code is :data:`REFUSAL_CODE`.
    """
    exc = CameraStreamError(WORDS["needed_yours" if yours else "needed"], code=REFUSAL_CODE)
    exc.yours = yours  # type: ignore[attr-defined]
    return exc


def block(*, printer_name: str | None = None, moment: str, yours: bool = False) -> dict[str, Any]:
    """What an agent reads when a camera is waiting on the add-on.

    *moment* is where it is being said: ``"snapshot"``, ``"setup"`` (a printer
    was just added; the message names it) or ``"health"``.  ``status`` is
    ``"needed"``; ``install`` names the call that adds it, after a yes, and
    ``command`` the line that adds it from a terminal on this computer.  The
    same block is said on every kind of install, including a copy of Kiln
    another program keeps up to date.
    """
    if moment not in MOMENTS:
        raise ValueError(f"moment must be one of {MOMENTS}, not {moment!r}")
    if moment == "setup":
        message = WORDS["setup"].format(printer=printer_name or "Your printer")
    else:
        message = WORDS["needed_yours" if yours else "needed"]
    return {
        "status": "needed",
        "message": message,
        "ask": WORDS["ask"],
        "install": {"tool": _INSTALL_TOOL["tool"], "arguments": dict(_INSTALL_TOOL["arguments"])},
        "command": command(),
        "free": True,
        "restart_required": False,
        "agent_note": _NOTE_NEEDED,
    }


def refusal_fields(exc: BaseException | None) -> dict[str, Any]:
    """``{RESULT_KEY: block}`` to ride beside a refusal with this add-on's code, else ``{}``."""
    if getattr(exc, "code", None) != REFUSAL_CODE:
        return {}
    return {RESULT_KEY: block(moment="snapshot", yours=bool(getattr(exc, "yours", False)))}


def setup_line(adapter: Any, *, door: str) -> str:
    """One line for the moment a printer is added, or ``""`` when nothing is needed.

    *door* is ``"cli"`` (a person reads it; it carries the command) or
    ``"tool"`` (an agent reads it; it names the call).
    """
    return _line(door) if needed_by(adapter) else ""


def setup_line_for_camera(camera: ExternalCamera, *, door: str) -> str:
    """:func:`setup_line` for a camera the person registered, with no adapter to ask."""
    return _line(door) if _camera_needs(camera) and ffmpeg_path() is None else ""


def _line(door: str) -> str:
    if door not in ("cli", "tool"):
        raise ValueError(f'door must be "cli" or "tool", not {door!r}')
    if door == "tool":
        return WORDS["tool_line"]
    return WORDS["cli_line"].format(command=command())


# ---------------------------------------------------------------------------
# Adding it
# ---------------------------------------------------------------------------


def install(
    *,
    confirm: bool,
    runner: Callable[..., Any] | None = None,
    find_spec: Callable[[str], object] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """Add the camera add-on to Kiln's own folder and say what happened.

    Nothing runs unless *confirm* is true, this is a person's own computer,
    and the add-on is not already found.  Kiln's own files and environment
    are never written, so it is safe while a print runs and on a copy of
    Kiln another program keeps up to date.  The folder appears only once the
    program in it has been run and shown to open a secure stream; a failed or
    interrupted install leaves nothing behind.

    ``status``: ``not_here`` | ``already_installed`` | ``needs_confirmation``
    | ``offline`` | ``failed`` | ``installed``.  Every result carries ``ok``,
    ``message`` (for the person), ``agent_note`` and ``restart_required``
    (always false).  *runner*, *find_spec* and *which* are injected in tests.
    """
    from kiln import self_update
    from kiln.update_nudge import _hosted_multitenant

    if _hosted_multitenant():
        return _result(
            "not_here",
            WORDS["not_here"],
            "This runs only on the person's own computer, never on Kiln's servers. "
            "Say `message`.",
        )
    if ffmpeg_path() is not None:
        return _result(
            "already_installed",
            WORDS["already"],
            "Nothing was changed. Take the snapshot now and show the picture.",
            ok=True,
        )
    if not confirm:
        return _result(
            "needs_confirmation",
            WORDS["confirm"],
            "Nothing was changed. Say `message`; only after the person says yes, call "
            'upgrade_kiln(addon="camera", confirm=true).',
        )

    final = addon_dir()
    staging = final.with_name(f"{final.name}.staging-{os.getpid()}")
    argvs = self_update.installer_argvs(
        PIP_REQUIREMENT, target=staging, find_spec=find_spec, which=which,
    )
    if not argvs:
        return _failed("This copy of Kiln has no installer it can run.")
    try:
        return _stage_and_place(argvs, staging=staging, final=final, runner=runner)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _stage_and_place(
    argvs: list[list[str]], *, staging: Path, final: Path, runner: Callable[..., Any] | None,
) -> dict[str, Any]:
    from kiln import self_update

    shutil.rmtree(staging, ignore_errors=True)
    try:
        staging.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _failed(str(exc))
    run = self_update.run_installer(argvs, runner=runner)
    installer = " ".join(run.argv or [])
    if not run.ok:
        if run.offline:
            return _result(
                "offline",
                WORDS["offline"],
                "Nothing was changed. Say `message` and try again later.",
                retryable=True,
                detail=run.tail,
                installer=installer,
            )
        return _failed(run.tail, installer=installer)

    problem = _staged_problem(staging, runner=runner)
    if problem:
        return _failed(problem, installer=installer)
    try:
        # One rename, so another look finds the whole add-on or none of it.
        if final.exists():
            shutil.rmtree(final)
        os.replace(staging, final)
    except OSError as exc:
        return _failed(str(exc), installer=installer)
    if ffmpeg_path() is None:
        shutil.rmtree(final, ignore_errors=True)
        return _failed("The add-on was placed but Kiln could not load it.", installer=installer)
    return _result(
        "installed",
        WORDS["added"],
        "Say `message`, then take the snapshot again now and show the picture. "
        "No restart is needed.",
        ok=True,
    )


def _staged_problem(staging: Path, *, runner: Callable[..., Any] | None) -> str:
    """Why the staged add-on cannot be used, or ``""`` when it runs and opens secure streams."""
    program = _program_in(staging)
    if program is None:
        return "The installer finished, but the add-on's program is not in what it installed."
    argv = [program, "-hide_banner", "-protocols"]
    try:
        proc = (runner or subprocess.run)(
            argv, capture_output=True, text=True, timeout=_CHECK_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 -- a program that cannot start is the finding
        return f"The add-on's program did not run on this computer ({exc})."
    if proc.returncode != 0:
        return "The add-on's program did not run on this computer."
    listed = f"{proc.stdout or ''}\n{proc.stderr or ''}".split()
    if _SECURE_PROTOCOL not in listed:
        return "The add-on's program cannot open a secure stream."
    return ""


def _program_in(folder: Path) -> str | None:
    """The program the package staged in *folder* names, leaving nothing loaded from there."""
    _forget_bundled()
    sys.path.insert(0, str(folder))
    try:
        return _bundled_program()
    finally:
        with contextlib.suppress(ValueError):
            sys.path.remove(str(folder))
        _forget_bundled()


def _failed(detail: str, *, installer: str = "") -> dict[str, Any]:
    line = command()
    return _result(
        "failed",
        WORDS["failed"].format(command=line),
        "Nothing was left behind. Relay `message` with `command` exactly as written. "
        f"`detail` and `installer` are for you only. {_NOTE_FOR_AGENT_ONLY}",
        command=line,
        detail=detail,
        installer=installer,
    )


def _result(
    status: str, message: str, agent_note: str, *, ok: bool = False, **extra: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": ok,
        "status": status,
        "addon": ADDON,
        "message": message,
        "agent_note": agent_note,
        "restart_required": False,
    }
    result.update({k: v for k, v in extra.items() if v not in ("", None)})
    return result
