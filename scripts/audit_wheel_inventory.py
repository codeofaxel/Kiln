#!/usr/bin/env python3
"""Verify the kiln3d wheel contains every data file end users need.

This gate exists to catch a single bug class: a ``package-data`` glob
that silently drops a subdirectory.  The 2026-05-19 incident landed
``[tool.setuptools.package-data] kiln = ["data/*.json", ...]`` — a
non-recursive glob — into v0.3.3 and shipped 3+ years of PyPI wheels
missing the ``design_knowledge/`` catalogs, the BOSL2 + MCAD OpenSCAD
libraries, and the Bambu A1 g-code wrappers.  Runtime code at
``_DATA_DIR / "<file>"`` gates on ``path.exists()`` so most callers
degraded silently to fallbacks; A1 connectivity hit a hard exception
because ``bambu_3mf.py`` raises when the gcode wrapper is missing.

The script builds the wheel into a temp dir, opens it as a zipfile,
and asserts presence + minimum counts for each critical group.  A
miss exits ``2`` with a clear per-file message; pass prints a
summary table and exits ``0``.

Run before every release.  Same family as ``audit_rls.py`` (security
gate) and ``check_doc_counts.py`` (stats gate) in kiln-pro.

Usage::

    python3 scripts/audit_wheel_inventory.py            # build + audit
    python3 scripts/audit_wheel_inventory.py --json     # CI format
    python3 scripts/audit_wheel_inventory.py \
        --package-dir kiln --outdir /tmp/wheel-audit    # override paths
    python3 scripts/audit_wheel_inventory.py \
        --wheel kiln/dist/kiln3d-1.1.2-py3-none-any.whl # audit an
                                                        # existing wheel

When ``--wheel`` is passed the script skips the build entirely and
audits the supplied artifact in place.  This is the right mode in CI
where the release workflow has already built the wheel that will go
to PyPI — auditing a rebuild would gate on a different binary than
the one being uploaded.

Exit codes:
* ``0`` — wheel inventory matches expectations
* ``1`` — config / build error (couldn't build, couldn't open wheel)
* ``2`` — wheel is missing files; release MUST be blocked
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# Repo root is the parent of this script's directory.  Default
# ``--package-dir`` is ``<repo>/kiln`` (where ``pyproject.toml`` lives).
_REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class GroupExpectation:
    """One audit assertion.

    A group is either ``mode="min_count"`` (at least N files match the
    glob — used for libraries that grow over time like BOSL2) or
    ``mode="exact_file"`` (one specific path must exist — used for
    files where absence is a hard runtime crash, e.g. A1 g-code).
    """

    name: str
    glob: str          # zipfile path relative to wheel root, e.g. "kiln/data/foo/*.json"
    mode: str          # "min_count" | "exact_file"
    min_count: int = 0  # used when mode == "min_count"
    why: str = ""       # one line — what breaks at runtime if this is missing


# The contract.  Each group is an assertion about the wheel.  When you
# add a new data file type under ``kiln/data/``, extend this list AND
# extend ``[tool.setuptools.package-data]`` in ``kiln/pyproject.toml``
# in the same commit — those two surfaces must stay in lockstep or the
# gate goes stale.
EXPECTED_GROUPS: list[GroupExpectation] = [
    # design_knowledge/ catalogs feed design_intelligence.py,
    # printability.py, assembly.py.  10 today; floor stays at 10.
    GroupExpectation(
        name="design_knowledge",
        glob="kiln/data/design_knowledge/*.json",
        mode="min_count",
        min_count=10,
        why="design_intelligence.py + printability.py read these at startup",
    ),
    # BOSL2 — third-party OpenSCAD library.  Resolved at runtime when
    # generated SCAD says `include <BOSL2/...>`.  56 .scad files today;
    # floor 50 leaves headroom for upstream pruning.
    GroupExpectation(
        name="BOSL2_scad",
        glob="kiln/data/scad_libraries/BOSL2/*.scad",
        mode="min_count",
        min_count=50,
        why="generated SCAD `include <BOSL2/...>` breaks without these",
    ),
    # MCAD — third-party OpenSCAD library.  36 .scad files today; floor
    # 30 leaves headroom for upstream pruning.  (Spec named 47; the
    # current upstream snapshot in the repo is 36.  Floor is the
    # commitment, not the current count.)
    GroupExpectation(
        name="MCAD_scad",
        glob="kiln/data/scad_libraries/MCAD/*.scad",
        mode="min_count",
        min_count=30,
        why="generated SCAD `include <MCAD/...>` breaks without these",
    ),
    # Bambu A1 g-code wrappers — bambu_3mf.py raises a hard exception
    # when these are missing.  Absence = no A1 connectivity for any
    # pip-installed user.
    GroupExpectation(
        name="bambu_a1_start_gcode",
        glob="kiln/data/bambu_a1_start_gcode.gcode",
        mode="exact_file",
        why="bambu_3mf.py raises if the start-gcode wrapper is missing",
    ),
    GroupExpectation(
        name="bambu_a1_end_gcode",
        glob="kiln/data/bambu_a1_end_gcode.gcode",
        mode="exact_file",
        why="bambu_3mf.py raises if the end-gcode wrapper is missing",
    ),
    # BOSL2 LICENSE — third-party legal compliance (BSD-2-Clause).
    # Shipping a third-party library without its license is an
    # attribution / redistribution violation; treat as exact_file.
    GroupExpectation(
        name="BOSL2_license",
        glob="kiln/data/scad_libraries/BOSL2/LICENSE",
        mode="exact_file",
        why="BSD-2-Clause attribution requires shipping LICENSE alongside the code",
    ),
]


# Top-level catalogs under kiln/data/.  Each is consumed by a specific
# subsystem; a missing one breaks the corresponding feature at runtime.
TOP_LEVEL_CATALOGS: list[tuple[str, str]] = [
    ("material_catalog.json", "design intelligence / material recommendation"),
    ("component_catalog.json", "assembly composition"),
    ("printer_intelligence.json", "printer recommendation"),
    ("safety_profiles.json", "safety gate"),
    ("slicer_profiles.json", "slicer profile resolution"),
    ("support_profiles.json", "support material estimation"),
    ("tool_safety.json", "tool-level safety advisor"),
    ("design_templates.json", "design template browser"),
]


def _build_wheel(package_dir: Path, outdir: Path) -> Path:
    """Build the wheel and return its path.

    Uses ``python -m build --wheel`` (matches what the release workflow
    does) so the audit exercises the same build path that ships to
    PyPI.  Re-running from a clean ``outdir`` keeps the audit
    deterministic — no stale wheels from a previous run.
    """
    cmd = [sys.executable, "-m", "build", "--wheel", "--outdir", str(outdir)]
    try:
        subprocess.run(
            cmd,
            cwd=str(package_dir),
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as e:
        sys.stderr.write(
            f"audit_wheel_inventory: {e}.  Install with `pip install build`.\n"
        )
        raise SystemExit(1)
    except subprocess.CalledProcessError as e:
        sys.stderr.write(
            "audit_wheel_inventory: wheel build failed.\n"
            f"  command: {' '.join(cmd)}\n"
            f"  cwd: {package_dir}\n"
            f"  exit: {e.returncode}\n"
            "--- stdout ---\n"
            f"{e.stdout}"
            "--- stderr ---\n"
            f"{e.stderr}"
        )
        raise SystemExit(1)

    wheels = sorted(outdir.glob("*.whl"))
    if not wheels:
        sys.stderr.write(
            f"audit_wheel_inventory: build produced no wheel in {outdir}\n"
        )
        raise SystemExit(1)
    if len(wheels) > 1:
        # If the outdir had stale wheels, prefer the newest by mtime
        # but warn — usually means the caller passed a non-temp
        # ``--outdir`` and forgot to clean it.
        sys.stderr.write(
            f"audit_wheel_inventory: outdir contains {len(wheels)} wheels; "
            f"auditing newest ({wheels[-1].name})\n"
        )
    return wheels[-1]


@dataclass
class GroupResult:
    name: str
    glob: str
    expected: int                 # 1 for exact_file, min_count for min_count
    found: int = 0
    matched: list[str] = field(default_factory=list)
    severity: str = "ok"          # "ok" | "missing"
    why: str = ""


def _audit_wheel(wheel_path: Path) -> tuple[list[GroupResult], list[GroupResult], int]:
    """Inspect ``wheel_path`` and return (group_results, catalog_results, total_entries).

    Group results cover the recursive globs (design_knowledge, BOSL2,
    MCAD, A1 gcode, BOSL2 LICENSE).  Catalog results cover the small
    fixed set of top-level ``kiln/data/*.json`` files — they're
    enumerated separately because each one maps to a named subsystem
    and a missing one needs to be called out by name, not by glob.
    """
    with zipfile.ZipFile(wheel_path) as zf:
        names = zf.namelist()
    name_set = set(names)

    group_results: list[GroupResult] = []
    for group in EXPECTED_GROUPS:
        if group.mode == "exact_file":
            present = group.glob in name_set
            group_results.append(GroupResult(
                name=group.name,
                glob=group.glob,
                expected=1,
                found=1 if present else 0,
                matched=[group.glob] if present else [],
                severity="ok" if present else "missing",
                why=group.why,
            ))
            continue

        # min_count: simple "starts-with prefix + endswith suffix" match.
        # Avoids a fnmatch import; the globs we use are all single-level
        # (no nested wildcards), so this is exact.
        prefix, _, suffix = group.glob.partition("*")
        matched = [
            n for n in names
            if n.startswith(prefix) and n.endswith(suffix) and "/" not in n[len(prefix):]
        ]
        group_results.append(GroupResult(
            name=group.name,
            glob=group.glob,
            expected=group.min_count,
            found=len(matched),
            matched=sorted(matched),
            severity="ok" if len(matched) >= group.min_count else "missing",
            why=group.why,
        ))

    catalog_results: list[GroupResult] = []
    for filename, role in TOP_LEVEL_CATALOGS:
        path = f"kiln/data/{filename}"
        present = path in name_set
        catalog_results.append(GroupResult(
            name=filename,
            glob=path,
            expected=1,
            found=1 if present else 0,
            matched=[path] if present else [],
            severity="ok" if present else "missing",
            why=f"consumed by {role}",
        ))

    return group_results, catalog_results, len(names)


def _badge(severity: str) -> str:
    return {"missing": "!!MISS", "ok": "  ok  "}.get(severity, "  ?   ")


def _render_human(
    wheel_path: Path,
    group_results: list[GroupResult],
    catalog_results: list[GroupResult],
    total_entries: int,
) -> str:
    """Single text table for both groups + top-level catalogs.

    Same layout style as ``audit_rls.py``: badge + name + numeric
    found/expected + the runtime "why" so an on-call engineer reading
    a failed gate at 3 AM knows what the missing file actually breaks.
    """
    lines: list[str] = []
    lines.append(f"Wheel: {wheel_path.name}  ({total_entries} entries)")
    lines.append("")
    header = ("CHECK", "FOUND", "EXPECTED", "RESULT")
    rows: list[tuple[str, str, str, str, str]] = []  # (severity, name, found, exp, why)
    for r in group_results:
        rows.append((
            r.severity,
            r.name,
            str(r.found),
            (str(r.expected) if r.expected > 1 else "present"),
            r.why,
        ))
    for r in catalog_results:
        rows.append((
            r.severity,
            f"data/{r.name}",
            str(r.found),
            "present",
            r.why,
        ))

    name_w = max(len(header[0]), max((len(row[1]) for row in rows), default=0))
    found_w = max(len(header[1]), max((len(row[2]) for row in rows), default=0))
    exp_w = max(len(header[2]), max((len(row[3]) for row in rows), default=0))
    fmt = f"{{:<{name_w}}}  {{:>{found_w}}}  {{:>{exp_w}}}  {{}}"
    lines.append(fmt.format(*header))
    for severity, name, found, exp, why in rows:
        lines.append(fmt.format(name, found, exp, f"{_badge(severity)} {why}"))
    return "\n".join(lines)


def _to_dict(r: GroupResult) -> dict[str, Any]:
    return {
        "name": r.name,
        "glob": r.glob,
        "expected": r.expected,
        "found": r.found,
        "severity": r.severity,
        "why": r.why,
        # Don't emit the full matched list in JSON to keep the payload
        # compact for CI logs; include just the first 3 as a sanity
        # tail for debugging missing-files reports.
        "matched_sample": r.matched[:3],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit the kiln3d wheel for missing data files.",
    )
    parser.add_argument(
        "--package-dir",
        type=Path,
        default=_REPO_ROOT / "kiln",
        help="Path to the directory containing pyproject.toml (default: <repo>/kiln)",
    )
    parser.add_argument(
        "--outdir",
        type=Path,
        default=None,
        help="Where to put the built wheel (default: a temp dir, cleaned on exit)",
    )
    parser.add_argument(
        "--wheel",
        type=Path,
        default=None,
        help=(
            "Path to an existing .whl to audit instead of building one.  "
            "Use in CI to audit the exact artifact about to be uploaded — "
            "auditing a rebuild would gate on a different binary than "
            "the one going to PyPI."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a human table",
    )
    args = parser.parse_args(argv)

    cleanup_outdir = False
    outdir: Path | None = None
    wheel_path: Path

    if args.wheel is not None:
        # Audit-existing mode.  Skip the build entirely; we just need
        # to open the supplied .whl.  --package-dir and --outdir are
        # both ignored in this mode (would be confusing if they were
        # silently applied).
        wheel_path = args.wheel.resolve()
        if not wheel_path.is_file() or wheel_path.suffix != ".whl":
            sys.stderr.write(
                f"audit_wheel_inventory: --wheel must point at an existing "
                f".whl file, got {wheel_path}\n"
            )
            return 1
    else:
        package_dir: Path = args.package_dir.resolve()
        if not (package_dir / "pyproject.toml").is_file():
            sys.stderr.write(
                f"audit_wheel_inventory: no pyproject.toml in {package_dir}\n"
            )
            return 1

        if args.outdir is None:
            outdir = Path(tempfile.mkdtemp(prefix="kiln-wheel-audit-"))
            cleanup_outdir = True
        else:
            outdir = args.outdir.resolve()
            outdir.mkdir(parents=True, exist_ok=True)

    try:
        if args.wheel is None:
            assert outdir is not None  # narrowing for type checker
            wheel_path = _build_wheel(args.package_dir.resolve(), outdir)
        group_results, catalog_results, total_entries = _audit_wheel(wheel_path)

        missing = [r for r in group_results + catalog_results if r.severity == "missing"]

        if args.json:
            print(json.dumps(
                {
                    "wheel": wheel_path.name,
                    "total_entries": total_entries,
                    "groups": [_to_dict(r) for r in group_results],
                    "catalogs": [_to_dict(r) for r in catalog_results],
                    "summary": {
                        "total_checks": len(group_results) + len(catalog_results),
                        "missing": len(missing),
                        "ok": (len(group_results) + len(catalog_results)) - len(missing),
                    },
                },
                indent=2,
            ))
        else:
            print(_render_human(wheel_path, group_results, catalog_results, total_entries))
            print()
            total = len(group_results) + len(catalog_results)
            print(f"Checked {total} groups.  Missing: {len(missing)}.  Ok: {total - len(missing)}.")
            if missing:
                print()
                print("MISSING FILES — release MUST be blocked:")
                for r in missing:
                    print(f"  - {r.glob}  ({r.why})")
                    if r.expected > 1:
                        print(f"      expected >= {r.expected}, found {r.found}")

        return 2 if missing else 0
    finally:
        if cleanup_outdir:
            # Best-effort cleanup; never let teardown failure mask the
            # real audit verdict.  If shutil errors (locked file on
            # Windows CI, etc.) we report-and-continue rather than
            # changing the exit code.
            try:
                shutil.rmtree(outdir, ignore_errors=True)
            except Exception as e:  # pragma: no cover
                sys.stderr.write(
                    f"audit_wheel_inventory: temp cleanup failed: {e!r}\n"
                )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
