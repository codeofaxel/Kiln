"""``kiln install-openscad`` — install the OpenSCAD development snapshot.

OpenSCAD is REQUIRED for Kiln's local OpenSCAD-native design path (the default
"make" loop), and it must be the *development snapshot*: the stable 2021-era
build silently breaks SVG/text booleans (an SVG logo in ``difference()`` yields
no geometry) and lacks the fast Manifold backend.

This command detects the OS and installs the snapshot non-interactively where it
can (Homebrew cask on macOS, snap edge on Linux), and prints the one manual step
where it can't — a GUI installer (Windows), a missing package manager, or a
package manager that needs a password with no human present.  It never pretends
to succeed: after any attempt it re-probes the live binary's version and tells
the truth.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import click

_SNAPSHOTS_URL = "https://openscad.org/downloads#snapshots"


def _current_openscad() -> tuple[str | None, str, int]:
    """Return ``(path, version, year)`` for an OpenSCAD on PATH (or the macOS
    ``.app``), or ``(None, "", 0)`` if none is found.

    Uses ``emboss_generator``'s NON-cached probe (``_detect_openscad_version``)
    deliberately: this command installs OpenSCAD and then re-checks, so the
    cached ``get_openscad_version`` would return the stale pre-install answer.
    """
    path = shutil.which("openscad")
    if not path and sys.platform == "darwin":
        mac = "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD"
        if os.path.isfile(mac) and os.access(mac, os.X_OK):
            path = mac
    if not path:
        return None, "", 0
    try:
        from kiln.emboss_generator import (
            _detect_openscad_version,
            _openscad_version_year,
        )

        ver = _detect_openscad_version(path)
        return path, ver, _openscad_version_year(ver)
    except Exception:  # noqa: BLE001 — never crash the installer over a probe
        return path, "", 0


def _min_year() -> int:
    try:
        from kiln.emboss_generator import _OPENSCAD_MIN_VERSION_YEAR

        return _OPENSCAD_MIN_VERSION_YEAR
    except Exception:  # noqa: BLE001
        return 2024


def _run(cmd: list[str]) -> tuple[bool, str]:
    """Run an install command; return ``(ok, combined_output)``.  Never raises."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except FileNotFoundError:
        return False, f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return False, f"{' '.join(cmd)}: timed out"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    return res.returncode == 0, ((res.stdout or "") + (res.stderr or "")).strip()


def _manual(min_year: int) -> None:
    click.echo("")
    click.echo(click.style("  Do this one step yourself (about a minute):", bold=True))
    click.echo(f"    Download the latest snapshot ({min_year} or newer) from:")
    click.echo(f"    {_SNAPSHOTS_URL}")
    click.echo("    Install it, then run:  kiln doctor")


@click.command("install-openscad")
@click.option(
    "--force",
    is_flag=True,
    help="Reinstall even if a current snapshot is already present.",
)
def install_openscad(force: bool) -> None:
    """Install the OpenSCAD development snapshot (Kiln's design engine).

    Kiln designs locally with OpenSCAD and needs the *development snapshot* — the
    regular/stable build is years old and silently breaks designs.  This detects
    your OS and installs the snapshot where it can, or shows you the one manual
    step where it can't.
    """
    min_year = _min_year()
    path, ver, year = _current_openscad()

    if path and year >= min_year and not force:
        click.echo(click.style(f"  OpenSCAD is already current ({ver}).", fg="green"))
        click.echo(f"  {path}")
        return

    if path and year and year < min_year:
        click.echo(
            click.style(
                f"  Found OpenSCAD {ver} — OUTDATED. Installing the {min_year}+ snapshot…",
                fg="yellow",
            )
        )
    elif path and not force:
        click.echo(
            "  Found OpenSCAD but couldn't read its version. "
            "Installing the snapshot to be safe…"
        )
    else:
        click.echo(f"  Installing the OpenSCAD development snapshot ({min_year}+)…")

    plat = sys.platform
    ok = False
    note = ""

    if plat == "darwin":
        if shutil.which("brew"):
            click.echo("  $ brew install --cask openscad@snapshot")
            ok, note = _run(["brew", "install", "--cask", "openscad@snapshot"])
        else:
            click.echo(
                click.style(
                    "  Homebrew isn't installed, so I can't auto-install on macOS.",
                    fg="yellow",
                )
            )
            _manual(min_year)
            return
    elif plat.startswith("linux"):
        if shutil.which("snap"):
            is_root = hasattr(os, "geteuid") and os.geteuid() == 0
            cmd = (["snap"] if is_root else ["sudo", "snap"]) + [
                "install",
                "openscad",
                "--edge",
            ]
            can_run = is_root or sys.stdin.isatty()
            if can_run:
                click.echo(f"  $ {' '.join(cmd)}")
                ok, note = _run(cmd)
            else:
                # sudo would block on a password prompt with no human present —
                # honest-degrade: hand the exact line to the user instead.
                click.echo(
                    click.style(
                        "  This needs admin (sudo) and no one is here to enter a password.",
                        fg="yellow",
                    )
                )
                click.echo("  Run this yourself, then re-run kiln doctor:")
                click.echo(f"    {' '.join(cmd)}")
                _manual(min_year)
                return
        else:
            click.echo(click.style("  No snap available on this Linux.", fg="yellow"))
            _manual(min_year)
            return
    elif plat in ("win32", "cygwin"):
        click.echo(
            click.style(
                "  On Windows the snapshot is a click-through installer I can't run for you.",
                fg="yellow",
            )
        )
        _manual(min_year)
        return
    else:
        click.echo(click.style(f"  Unsupported platform: {plat}.", fg="yellow"))
        _manual(min_year)
        return

    # Re-probe honestly — did the install actually land a current snapshot?
    path, ver, year = _current_openscad()
    if ok and path and year >= min_year:
        click.echo(click.style(f"  ✓ OpenSCAD {ver} installed and current.", fg="green"))
        click.echo(f"  {path}")
    elif ok and path:
        click.echo(
            click.style(
                f"  Installed OpenSCAD ({ver or 'unknown version'}), but couldn't "
                f"confirm it's the {min_year}+ snapshot.",
                fg="yellow",
            )
        )
        _manual(min_year)
    else:
        click.echo(click.style("  Automatic install didn't complete.", fg="yellow"))
        if note:
            click.echo("  " + note.splitlines()[-1][:200])
        _manual(min_year)


def register_install_openscad_cli(cli_group: click.Group) -> None:
    """Attach ``kiln install-openscad`` to the CLI group."""
    cli_group.add_command(install_openscad)
