"""``kiln install-step-backend`` — install the STEP → mesh converter.

Kiln reads STEP/STP files (the format every CAD package and every machine
shop speaks) by converting them to a mesh, and that needs a CAD kernel.  It
will happily use FreeCAD or Gmsh if they are already on the machine, but
neither is something we can install for somebody: FreeCAD is a GUI package
behind a platform-specific package manager, and Gmsh is mesh-only.

The OCCT kernel is.  ``cadquery-ocp-novtk`` ships a wheel for every Python
and platform Kiln supports, so a plain pip install works everywhere — no
package manager, no GUI installer, no admin password.  That is the whole
reason this command can exist.

It installs the VTK-free KERNEL, which is neither of the two obvious
choices.  Measured on disk 2026-07-27: ``cadquery`` is 1163 MB, and even
``cadquery-ocp`` is 848 MB because it hard-requires ``vtk==9.6.2`` and won't
import without it.  ``cadquery-ocp-novtk`` is the same OCCT 7.9.3.1.1 build
at 228 MB, converts identically, and does it about twice as fast.  Nobody
should download a gigabyte to open one STEP file.

It installs into the interpreter running Kiln (``sys.executable``), not
whatever ``pip`` happens to be first on PATH, so a pipx/uv/venv install gets
the backend in the environment that will actually look for it.  Like
``install-openscad``, it never pretends to succeed: after the attempt it
re-probes the live backend and tells the truth.
"""
from __future__ import annotations

import subprocess
import sys

import click

_DOCS_URL = "https://kiln3d.com/docs/step"

# pip refuses to touch a distro-managed interpreter (PEP 668).  The fix is
# the user's call, not ours to force — we surface the exact line instead.
_EXTERNALLY_MANAGED = "externally-managed-environment"


def _probe() -> tuple[bool, str | None]:
    """Return ``(any_available, name_of_backend_in_use)``.

    Imported lazily and re-called after the install so the answer is the
    live one, never a cached pre-install snapshot.
    """
    try:
        from kiln.step_import import check_step_support

        info = check_step_support()
    except Exception:  # noqa: BLE001 — a probe must never crash the installer
        return False, None
    if not info.get("any_available"):
        return False, None
    ranked = sorted(
        (n for n, b in info["backends"].items() if b.get("available")),
        key=lambda n: info["backends"][n].get("priority", 99),
    )
    return True, (ranked[0] if ranked else None)


def _run(cmd: list[str]) -> tuple[bool, str]:
    """Run an install command; return ``(ok, combined_output)``.  Never raises."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except FileNotFoundError:
        return False, f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return False, f"{' '.join(cmd)}: timed out"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    return res.returncode == 0, ((res.stdout or "") + (res.stderr or "")).strip()


def _manual(cmd: list[str]) -> None:
    click.echo("")
    click.echo(click.style("  Do this one step yourself:", bold=True))
    click.echo(f"    {' '.join(cmd)}")
    click.echo("    Then run:  kiln install-step-backend  (to confirm)")
    click.echo(f"    Details:   {_DOCS_URL}")


@click.command("install-step-backend")
@click.option(
    "--force",
    is_flag=True,
    help="Install even if a working backend is already present.",
)
def install_step_backend(force: bool) -> None:
    """Install the CAD kernel Kiln needs to read STEP/STP files.

    STEP is what CAD packages and machine shops exchange.  Kiln converts it
    to a mesh so the rest of the pipeline (diagnose, slice, print) can work
    on it, and that conversion needs a CAD kernel.  This installs one.
    """
    from kiln.step_import import PIP_BACKEND

    available, backend = _probe()
    if available and not force:
        click.echo(
            click.style(f"  STEP import already works (backend: {backend}).", fg="green")
        )
        click.echo("  Try it:  kiln mcp  →  import_step_file")
        return

    if available and force:
        click.echo(f"  Backend '{backend}' already present — reinstalling anyway…")
    else:
        click.echo(
            "  Installing the STEP converter (the OCCT CAD kernel — "
            "~68 MB download, ~228 MB on disk)…"
        )

    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", PIP_BACKEND]
    click.echo(f"  $ {' '.join(cmd)}")
    ok, note = _run(cmd)

    # PEP 668: a distro-managed Python refuses the install.  Adding
    # --break-system-packages for the user would be us overriding their OS
    # packaging on their behalf — their call, so hand them the line.
    if not ok and _EXTERNALLY_MANAGED in note:
        click.echo(
            click.style(
                "  This Python is managed by your operating system, so pip won't\n"
                "  install into it without you saying so explicitly.",
                fg="yellow",
            )
        )
        click.echo("  Safest: install Kiln in a virtualenv, or with pipx/uv.")
        click.echo("  Or, if you're sure, re-run pip with:")
        _manual(cmd + ["--break-system-packages"])
        return

    # Re-probe honestly — pip exiting 0 is not proof the backend imports.
    available, backend = _probe()
    if ok and available:
        click.echo(
            click.style(f"  ✓ STEP import is working (backend: {backend}).", fg="green")
        )
        click.echo("  Try it:  kiln mcp  →  import_step_file")
        return

    if ok and not available:
        click.echo(
            click.style(
                f"  pip installed {PIP_BACKEND}, but Kiln still can't load a STEP\n"
                "  backend. That usually means Kiln is running from a different\n"
                "  Python than the one just installed into.",
                fg="yellow",
            )
        )
        click.echo(f"  Kiln's interpreter:  {sys.executable}")
        _manual(cmd)
        return

    click.echo(click.style("  Automatic install didn't complete.", fg="yellow"))
    if note:
        click.echo("  " + note.splitlines()[-1][:200])
    _manual(cmd)


def register_install_step_backend_cli(cli_group: click.Group) -> None:
    """Attach ``kiln install-step-backend`` to the CLI group."""
    cli_group.add_command(install_step_backend)
