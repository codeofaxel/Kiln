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

The groups below are a hand-written list, and a list only sees what
somebody thought to write on it.  ``pro_tool_manifest.json`` sat beside
``server.py``, was read by it at every start, and was in no release from
1.1.3 to 1.4.1.1 because it was on neither list -- not ``package-data``
and not this one -- so every pip install registered no served tools and
said so at DEBUG.  The DERIVED half of this gate does not need the list:

* every file the package source tracks must be in the wheel, unless
  ``SOURCE_EXCLUSIONS`` says why not (an exclusion that matches nothing
  fails, so the list cannot outlive what it describes); and
* every file the code reads beside itself -- ``Path(__file__).parent /
  "..."``, ``os.path.join(os.path.dirname(__file__), ...)``,
  ``importlib.resources.files("kiln") / ...`` -- must be in the wheel,
  named with the line that reads it.  No exclusion applies to these.

A new file is required by default.  Nobody has to remember this gate.

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
import ast
import fnmatch
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
    # The print-code banner's own notifier (kiln/scripts/build_notifier.py).
    # Missing, a Mac's banner arrives as "Script Editor" and a Windows toast
    # loses Kiln's icon -- nothing crashes, which is why only this gate sees it.
    GroupExpectation(
        name="mac_notifier_helper",
        glob="kiln/data/notifier/Kiln.app/Contents/MacOS/kiln-notifier",
        mode="exact_file",
        why="print codes on a Mac post as Kiln only through this helper",
    ),
    GroupExpectation(
        name="mac_notifier_signature",
        glob="kiln/data/notifier/Kiln.app/Contents/_CodeSignature/CodeResources",
        mode="exact_file",
        why="a Mac refuses to run the helper with its signature missing",
    ),
    GroupExpectation(
        name="windows_notifier_icon",
        glob="kiln/data/notifier/Kiln.png",
        mode="exact_file",
        why="Windows toasts show Kiln's icon from this file",
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


# ---------------------------------------------------------------------------
# The derived inventory
# ---------------------------------------------------------------------------

# Tracked files that deliberately stay out of the wheel, as
# ``(fnmatch pattern over the wheel path, why)``.  ``*`` crosses directory
# separators here.  Everything else the source tree tracks must ship.  A
# pattern that matches no tracked file is reported as stale.
SOURCE_EXCLUSIONS: list[tuple[str, str]] = [
    (
        "kiln/data/scad_libraries/*/.github/*",
        "the upstream library's own repository automation; nothing reads it",
    ),
    (
        "kiln/data/scad_libraries/*/.gitignore",
        "the upstream library's own repository settings",
    ),
    (
        "kiln/data/scad_libraries/BOSL2/.openscad_*_rc",
        "settings for the upstream library's documentation generator",
    ),
    (
        "kiln/data/scad_libraries/BOSL2/*.md",
        "the upstream library's contributor documentation; its LICENSE ships",
    ),
    (
        "kiln/data/scad_libraries/BOSL2/resources/*",
        "assets for the upstream library's documentation site",
    ),
    (
        "kiln/data/scad_libraries/MCAD/TODO",
        "the upstream library's own work list",
    ),
    (
        "kiln/data/scad_libraries/MCAD/bitmap/README",
        "usage notes for the upstream bitmap examples; the .scad files ship",
    ),
]

# Never part of a package, tracked or not.  Only consulted when the tree is
# walked because git could not list it.
_WALK_JUNK_DIRS = {"__pycache__", ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
_WALK_JUNK_SUFFIXES = {".pyc", ".pyo"}
_WALK_JUNK_NAMES = {".DS_Store"}


def _source_files(src_root: Path, package: str = "kiln") -> list[str]:
    """Every file the package source holds, as wheel paths (``kiln/...``).

    Git's list when the tree is a checkout -- what is tracked is what a
    release is cut from, and it leaves out caches without a rule here.  A
    tree git cannot list (an unpacked sdist, a test fixture) is walked.
    """
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z", "--", package],
            cwd=str(src_root), capture_output=True, text=True, check=False,
        )
        tracked = [n for n in listed.stdout.split("\0") if n] if listed.returncode == 0 else []
    except OSError:
        tracked = []
    if tracked:
        return sorted(n for n in tracked if (src_root / n).is_file())

    found: list[str] = []
    for path in (src_root / package).rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(src_root)
        if _WALK_JUNK_DIRS.intersection(rel.parts):
            continue
        if path.suffix in _WALK_JUNK_SUFFIXES or path.name in _WALK_JUNK_NAMES:
            continue
        found.append(rel.as_posix())
    return sorted(found)


_PATH_CTORS = {"Path", "PurePath", "PosixPath", "_Path"}
_SAME_PATH_METHODS = {"resolve", "absolute", "expanduser"}
_SAME_PATH_FUNCS = {"abspath", "realpath", "normpath", "fspath", "str"}


class _FileAnchoredPaths:
    """The paths one module builds from where it sits on disk.

    Follows ``__file__`` (its own, or an imported package module's) through
    ``Path(...)``, ``.resolve()``, ``.parent``, ``.parents[n]``, ``/ "name"``,
    ``os.path.dirname`` / ``os.path.join``, and ``resources.files("pkg")``,
    and through names assigned such a path anywhere in the file.  A step it
    cannot follow -- a name built at run time -- ends the path there; the
    tracked-file half of the gate covers whatever that reaches.
    """

    def __init__(self, module_path: Path, src_root: Path) -> None:
        self.module_path = module_path
        self.src_root = src_root
        self.names: dict[str, Path] = {}
        self.modules: dict[str, Path] = {}

    def _module_file(self, dotted: str) -> Path | None:
        base = self.src_root.joinpath(*dotted.split("."))
        if (base / "__init__.py").is_file():
            return base / "__init__.py"
        if base.with_suffix(".py").is_file():
            return base.with_suffix(".py")
        return None

    def learn_imports(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    target = self._module_file(alias.name)
                    if target is not None and alias.asname:
                        self.modules[alias.asname] = target
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                for alias in node.names:
                    target = self._module_file(f"{node.module}.{alias.name}")
                    if target is not None:
                        self.modules[alias.asname or alias.name] = target

    def learn_names(self, tree: ast.AST) -> None:
        # Assignments are read to a fixed point so a name built from an
        # earlier name resolves whatever order ast.walk visits them in.
        for _ in range(4):
            before = len(self.names)
            for node in ast.walk(tree):
                value = getattr(node, "value", None)
                if isinstance(node, ast.Assign):
                    targets = node.targets
                elif isinstance(node, ast.AnnAssign):
                    targets = [node.target]
                else:
                    continue
                resolved = self.resolve(value) if value is not None else None
                if resolved is None:
                    continue
                for target in targets:
                    if isinstance(target, ast.Name):
                        self.names.setdefault(target.id, resolved)
            if len(self.names) == before:
                break

    def resolve(self, node: ast.AST | None) -> Path | None:
        if isinstance(node, ast.Name):
            if node.id == "__file__":
                return self.module_path
            return self.names.get(node.id)
        if isinstance(node, ast.Attribute):
            if node.attr == "__file__" and isinstance(node.value, ast.Name):
                return self.modules.get(node.value.id)
            if node.attr == "parent":
                base = self.resolve(node.value)
                return base.parent if base is not None else None
            return None
        if isinstance(node, ast.Subscript):
            target = node.value
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "parents"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, int)
            ):
                base = self.resolve(target.value)
                if base is not None and 0 <= node.slice.value < len(base.parents):
                    return base.parents[node.slice.value]
            return None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            base = self.resolve(node.left)
            if base is not None and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str):
                return base / node.right.value
            return None
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            args = node.args
            if name in _SAME_PATH_METHODS and isinstance(func, ast.Attribute) and not args:
                return self.resolve(func.value)
            if name in _PATH_CTORS or name in _SAME_PATH_FUNCS:
                return self.resolve(args[0]) if len(args) == 1 else None
            if name == "dirname" and len(args) == 1:
                base = self.resolve(args[0])
                return base.parent if base is not None else None
            if name in ("join", "joinpath") and args:
                if name == "joinpath" and isinstance(func, ast.Attribute):
                    base, parts = self.resolve(func.value), args
                else:
                    base, parts = self.resolve(args[0]), args[1:]
                if base is None:
                    return None
                for part in parts:
                    if not (isinstance(part, ast.Constant) and isinstance(part.value, str)):
                        return None
                    base = base / part.value
                return base
            if name == "files" and len(args) == 1 and isinstance(args[0], ast.Constant):
                target = self._module_file(str(args[0].value))
                return target.parent if target is not None else None
        return None


def _code_read_files(src_root: Path, package: str = "kiln") -> dict[str, list[str]]:
    """Files the code reads beside itself: ``{wheel path: ["module.py:line"]}``.

    Only paths that exist as non-Python files inside the package count.  A
    path the code probes that the tree does not hold (an optional file) is
    not a packaging question; a directory is covered file by file by the
    tracked-file half.
    """
    package_root = (src_root / package).resolve()
    vendored = package_root / "data"
    reads: dict[str, list[str]] = {}
    for module_path in sorted(package_root.rglob("*.py")):
        if vendored in module_path.parents or "__pycache__" in module_path.parts:
            continue
        try:
            tree = ast.parse(module_path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        scope = _FileAnchoredPaths(module_path, src_root.resolve())
        scope.learn_imports(tree)
        scope.learn_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.BinOp, ast.Call)):
                continue
            target = scope.resolve(node)
            if target is None or target.suffix == ".py":
                continue
            try:
                inside = target.resolve().relative_to(package_root)
            except (OSError, ValueError):
                continue
            if not (package_root / inside).is_file():
                continue
            where = f"{module_path.relative_to(src_root.resolve()).as_posix()}:{node.lineno}"
            readers = reads.setdefault(f"{package}/{inside.as_posix()}", [])
            if where not in readers:
                readers.append(where)
    return reads


@dataclass
class DerivedResult:
    """What the source tree says the wheel must hold, against what it holds."""

    source_files: int = 0
    code_reads: int = 0
    excluded: int = 0
    # (wheel path, why it is required)
    missing: list[tuple[str, str]] = field(default_factory=list)
    # (pattern, why) for exclusions that no longer match any tracked file
    stale_exclusions: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing and not self.stale_exclusions


def _audit_derived(
    wheel_names: set[str],
    package_dir: Path,
    exclusions: list[tuple[str, str]] | None = None,
) -> DerivedResult:
    """Compare the wheel with what ``package_dir``'s source says it needs."""
    exclusions = SOURCE_EXCLUSIONS if exclusions is None else exclusions
    src_root = package_dir / "src"
    source = _source_files(src_root)
    reads = _code_read_files(src_root)

    result = DerivedResult(source_files=len(source), code_reads=len(reads))
    used: set[str] = set()
    for name in source:
        if name in reads:
            continue  # judged below, where no exclusion applies
        pattern = next((p for p, _ in exclusions if fnmatch.fnmatchcase(name, p)), None)
        if pattern is not None:
            used.add(pattern)
            result.excluded += 1
            continue
        if name not in wheel_names:
            result.missing.append((name, "in the source tree, on no exclusion"))
    for name, readers in sorted(reads.items()):
        if name not in wheel_names:
            result.missing.append((name, f"read by {', '.join(readers[:3])}"))
    result.missing.sort()
    result.stale_exclusions = [(p, why) for p, why in exclusions if p not in used]
    return result


def _build_wheel(package_dir: Path, outdir: Path) -> Path:
    """Build the wheel and return its path.

    Uses ``python -m build --wheel`` (matches what the release workflow
    does) so the audit exercises the same build path that ships to
    PyPI, and falls back to ``pip wheel`` where ``build`` is not
    installed -- the same backend either way.  Re-running from a clean
    ``outdir`` keeps the audit deterministic — no stale wheels from a
    previous run.
    """
    attempts = [
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(outdir)],
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--quiet",
         "--wheel-dir", str(outdir), "."],
    ]
    failures: list[str] = []
    for cmd in attempts:
        done = subprocess.run(
            cmd, cwd=str(package_dir), check=False, capture_output=True, text=True,
        )
        if done.returncode == 0:
            break
        failures.append(
            f"  command: {' '.join(cmd)}\n"
            f"  exit: {done.returncode}\n"
            f"{(done.stdout + done.stderr)[-1500:]}"
        )
        if "No module named" not in done.stderr:
            break  # the tool ran and the build itself failed; say so
    else:
        done = None
    if done is None or done.returncode != 0:
        sys.stderr.write(
            "audit_wheel_inventory: wheel build failed.\n"
            f"  cwd: {package_dir}\n" + "\n".join(failures) + "\n"
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


def _render_derived(derived: DerivedResult) -> str:
    """The derived half, with the one edit that fixes each finding."""
    lines = [
        (
            f"Derived from the source tree: {derived.source_files} files, "
            f"{derived.code_reads} read by code beside itself, "
            f"{derived.excluded} excluded with a reason."
        ),
    ]
    if derived.missing:
        lines.append("")
        lines.append("NOT IN THE WHEEL — release MUST be blocked:")
        for name, why in derived.missing:
            lines.append(f"  - {name}  ({why})")
        lines.append(
            "  Add each to [tool.setuptools.package-data] in kiln/pyproject.toml "
            "(paths there are relative to kiln/).  A file that should stay out "
            "goes in SOURCE_EXCLUSIONS in this script, with why -- unless code "
            "reads it, in which case it ships."
        )
    if derived.stale_exclusions:
        lines.append("")
        lines.append("EXCLUSIONS THAT MATCH NOTHING — delete them:")
        for pattern, why in derived.stale_exclusions:
            lines.append(f"  - {pattern}  ({why})")
    if derived.ok:
        lines.append("Every one of them that should ship is in the wheel.")
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
        with zipfile.ZipFile(wheel_path) as zf:
            derived = _audit_derived(set(zf.namelist()), args.package_dir.resolve())

        missing = [r for r in group_results + catalog_results if r.severity == "missing"]

        if args.json:
            print(json.dumps(
                {
                    "wheel": wheel_path.name,
                    "total_entries": total_entries,
                    "groups": [_to_dict(r) for r in group_results],
                    "catalogs": [_to_dict(r) for r in catalog_results],
                    "derived": {
                        "source_files": derived.source_files,
                        "code_reads": derived.code_reads,
                        "excluded": derived.excluded,
                        "missing": [{"path": n, "why": why} for n, why in derived.missing],
                        "stale_exclusions": [p for p, _ in derived.stale_exclusions],
                    },
                    "summary": {
                        "total_checks": len(group_results) + len(catalog_results),
                        "missing": len(missing) + len(derived.missing),
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
            print()
            print(_render_derived(derived))

        return 2 if missing or not derived.ok else 0
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
