#!/usr/bin/env python3
"""Install kiln3d the way a user does, then prove the core features RUN.

WHY THIS EXISTS
---------------
``ci.yml`` installs ``-r kiln/requirements-lock.txt`` and then
``pip install -e ./kiln --no-deps``.  The lock file carries the full
transitive set, so a package that Kiln imports at runtime but does not
DECLARE in ``kiln/pyproject.toml`` is present in every CI job and absent
for every real user.  CI is structurally blind to that, and so is the
test suite, because both run against an environment somebody else
populated.

It shipped.  v1.4.1 announced that previews get the studio look "on every
computer" and the module that paints them, :mod:`kiln.stage_paint`,
imports numpy, Pillow and trimesh through a ``_deps()`` helper that
returns ``None`` when any import fails.  Declining is SILENT by design —
the caller just gets the OpenSCAD look.  numpy and trimesh were core
dependencies; Pillow was only in the ``emboss`` extra.  So the headline
feature of the release was inert for everyone who installed the
documented way, nothing was red anywhere, and the module's own docstring
asserted the opposite.

Measured against published 1.4.1: a default ``pip install kiln3d``
rendered ``renderer="openscad"``; installing Pillow alone flipped the
identical call to ``renderer="stage_paint"``.

THE SHAPE OF THE BUG, WHICH IS THE POINT
----------------------------------------
This is not "a dependency was missing".  It was DECLARED, in an extra,
and the code degraded GRACEFULLY.  Every individual piece was defensible;
the composition was a feature that could never run.  A gate that only
asks "is every import declared somewhere?" passes it.  The only question
that catches it is behavioural: *on the install a user actually gets,
does the capability happen?*

So this gate does not read metadata.  It builds the package, installs it
into an empty virtualenv with no lock file and no extras, points ``HOME``
at an empty directory (no Playwright cache, no ``~/.kiln``), and runs the
capability.  A probe asserts the ENGINE that served the result, not just
that the call returned without raising — "it didn't crash" is exactly the
answer a silent fallback gives.

Deliberately NOT in the test suite: pytest runs inside the environment
this gate exists to distrust.

Usage::

    python3 scripts/audit_clean_install.py           # build, install, probe
    python3 scripts/audit_clean_install.py --keep    # leave the venv for poking
    python3 scripts/audit_clean_install.py --json    # CI format

Exit 0 = every probe served by the expected engine.  Exit 2 = a core
capability is inert on a default install.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "kiln"


# ---------------------------------------------------------------------------
# The probes
# ---------------------------------------------------------------------------
#
# Each probe is a snippet run by the CLEAN interpreter.  It must print one
# JSON object with at least {"ok": bool, "detail": str}.  Keep them to
# capabilities the product ADVERTISES as working out of the box — this
# gate's cost is a venv build, so it earns its place by covering the
# promises, not the internals.

_TINY_STL = """
import struct, pathlib
tri = (struct.pack("<fff", 0, 0, 1)
       + struct.pack("<fff", 0, 0, 0)
       + struct.pack("<fff", 40, 0, 0)
       + struct.pack("<fff", 0, 40, 0)
       + struct.pack("<H", 0))
p = pathlib.Path(OUT) / "probe.stl"
p.write_bytes(b"\\x00" * 80 + struct.pack("<I", 1) + tri)
"""

PROBES: list[dict] = [
    {
        "name": "import",
        "why": "the package imports at all on a bare install",
        "code": """
import json, kiln
print(json.dumps({"ok": bool(kiln.__version__), "detail": kiln.__version__}))
""",
    },
    {
        "name": "studio-preview",
        "why": (
            "a preview renders through the software stage painter, which is "
            "what a machine with no browser gets -- the 1.4.1 regression"
        ),
        "code": f"""
import json, os, pathlib
OUT = os.environ["PROBE_DIR"]
{_TINY_STL}
from kiln.model_visualizer import visualize_model
r = visualize_model(str(p), angles=["isometric"],
                    output_dir=str(pathlib.Path(OUT) / "render"),
                    share_link=False)
served = r.get("renderer")
# "openscad" is the honest fallback for a machine that genuinely cannot
# paint -- but this venv HAS numpy/Pillow/trimesh if they are declared,
# so falling back here means a declared-dependency gap.  There is no
# OpenSCAD in this environment either, so the fallback cannot even draw.
ok = bool(r.get("success")) and served == "stage_paint"
print(json.dumps({{"ok": ok, "detail": f"success={{r.get('success')}} renderer={{served}}"}}))
""",
    },
    {
        "name": "stage-payload",
        "why": "the 3D stage panel every tier gets can read a mesh (trimesh)",
        "code": f"""
import json, os, pathlib
OUT = os.environ["PROBE_DIR"]
{_TINY_STL}
from kiln.mesh_payload import mesh_to_viewer_payload
payload = mesh_to_viewer_payload(str(p))
ok = bool(payload) and bool(payload.get("positions"))
print(json.dumps({{"ok": ok, "detail": f"payload keys={{sorted(payload)[:4] if payload else None}}"}}))
""",
    },
]


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kw)


def build_and_install(workdir: Path) -> tuple[Path, str | None]:
    """A venv holding ONLY what `pip install kiln3d` would pull in."""
    venv = workdir / "venv"
    made = _run([sys.executable, "-m", "venv", str(venv)])
    if made.returncode != 0:
        return venv, f"could not create a virtualenv: {made.stderr.strip()[:400]}"

    py = venv / "bin" / "python"
    if not py.exists():  # Windows layout
        py = venv / "Scripts" / "python.exe"

    # No lock file, no extras, no --no-deps: resolve exactly the metadata
    # the published package carries.  That is the whole point.
    got = _run([str(py), "-m", "pip", "install", "-q", str(PKG)])
    if got.returncode != 0:
        return venv, f"pip install failed:\n{got.stdout[-800:]}{got.stderr[-800:]}"
    return venv, None


def probe(venv: Path, workdir: Path, spec: dict) -> dict:
    py = venv / "bin" / "python"
    if not py.exists():
        py = venv / "Scripts" / "python.exe"

    # An empty HOME is the second half of "a machine that is not this one":
    # no Playwright browser cache, no ~/.kiln, no prior state to lean on.
    home = workdir / "home"
    home.mkdir(exist_ok=True)
    probe_dir = workdir / f"probe-{spec['name']}"
    probe_dir.mkdir(exist_ok=True)

    env = {
        "HOME": str(home),
        "PATH": f"{venv / 'bin'}:/usr/bin:/bin",
        "PROBE_DIR": str(probe_dir),
        "KILN_TELEMETRY_DISABLED": "1",
    }
    res = _run([str(py), "-c", spec["code"]], env=env)
    line = next(
        (ln for ln in reversed(res.stdout.splitlines()) if ln.startswith("{")), ""
    )
    try:
        out = json.loads(line)
    except ValueError:
        return {
            "name": spec["name"],
            "ok": False,
            "detail": (res.stderr or res.stdout or "no output")[-500:],
            "why": spec["why"],
        }
    return {
        "name": spec["name"],
        "ok": bool(out.get("ok")),
        "detail": str(out.get("detail", "")),
        "why": spec["why"],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--keep", action="store_true", help="keep the venv for poking")
    args = ap.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="kiln-clean-install-"))
    try:
        venv, err = build_and_install(workdir)
        if err:
            if args.json:
                print(json.dumps({"ok": False, "error": err}))
            else:
                print(f"clean-install audit could not run:\n{err}", file=sys.stderr)
            # An environment we cannot build is not a pass.
            return 2

        results = [probe(venv, workdir, spec) for spec in PROBES]
        failed = [r for r in results if not r["ok"]]

        if args.json:
            print(json.dumps({"ok": not failed, "probes": results}, indent=2))
        else:
            print("Clean-install capability audit")
            print("=" * 60)
            print(f"installed: pip install {PKG}  (no lock file, no extras)")
            print("environment: fresh venv, empty HOME\n")
            for r in results:
                mark = "✓" if r["ok"] else "✗"
                print(f"  {mark} {r['name']}: {r['detail']}")
                if not r["ok"]:
                    print(f"      expected: {r['why']}")
            print()
            if failed:
                print(
                    f"FAIL — {len(failed)} core capability(ies) do not work on the "
                    "install a user actually gets.\n"
                    "A capability that declines silently reports no error and no "
                    "failing test; it simply never happens.  Check that every "
                    "import it needs is in kiln/pyproject.toml `dependencies`, "
                    "not in an extra."
                )
            else:
                print("PASS — every probed capability ran on a default install.")
        return 2 if failed else 0
    finally:
        if args.keep:
            print(f"(venv kept at {workdir})", file=sys.stderr)
        else:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
