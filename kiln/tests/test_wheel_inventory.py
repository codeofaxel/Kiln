"""The wheel carries every file the package tracks and every file the code reads.

Replays the bug class behind two shipped misses.  2026-05-19: a non-recursive
``data/*.json`` glob dropped ``design_knowledge/``, BOSL2, MCAD and the A1
g-code wrappers from every wheel since v0.3.3.  2026-10-02: ``server.py``
read ``pro_tool_manifest.json`` beside itself at every start, the file was on
no package-data list, and no release from 1.1.3 to 1.4.1.1 carried it -- so
every pip install registered no served tools and said so at DEBUG.  The
publish-time audit that followed the first miss checked a hand-written list,
which is why it could not see the second.

The gate under test (``scripts/audit_wheel_inventory.py``) derives what must
ship instead of listing it: every tracked package file (unless an exclusion
says why, and an exclusion that matches nothing fails) and every file the
code builds a path to from ``__file__``.  The first half of this file proves
each of those checks CAN fail, on a synthetic tree; the last test builds the
real wheel from this tree and runs the real audit over it.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import zipfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "audit_wheel_inventory.py"
_PACKAGE_DIR = _REPO_ROOT / "kiln"


def _load_gate():
    spec = importlib.util.spec_from_file_location("audit_wheel_inventory", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["audit_wheel_inventory"] = mod
    spec.loader.exec_module(mod)
    return mod


gate = _load_gate()


# ---------------------------------------------------------------------------
# A synthetic package, shaped like the real one where it matters
# ---------------------------------------------------------------------------

_SERVER_PY = '''
from pathlib import Path
import os
import importlib.resources as resources
import kiln.helper as _helper

_DATA_DIR = Path(__file__).resolve().parent / "data"

def stubs():
    manifest_path = Path(__file__).parent / "pro_tool_manifest.json"
    return manifest_path.exists()

def catalog():
    return _DATA_DIR / "catalog.json"

def joined():
    return os.path.join(os.path.dirname(__file__), "data", "joined.json")

def other_module():
    return os.path.join(os.path.dirname(_helper.__file__), "beside_helper.txt")

def packaged():
    return resources.files("kiln") / "data" / "resourced.json"

def dynamic(name):
    return _DATA_DIR / f"{name}.gcode"   # not followed: the tree half covers it
'''


def _make_tree(tmp_path: Path) -> Path:
    """``<tmp>/pkg/src/kiln/...`` -- a package dir with a src layout."""
    pkg = tmp_path / "pkg"
    src = pkg / "src" / "kiln"
    (src / "data" / "deep").mkdir(parents=True)
    (src / "__init__.py").write_text("")
    (src / "helper.py").write_text("x = 1\n")
    (src / "server.py").write_text(_SERVER_PY)
    (src / "pro_tool_manifest.json").write_text("{}")
    (src / "beside_helper.txt").write_text("")
    (src / "data" / "catalog.json").write_text("{}")
    (src / "data" / "joined.json").write_text("{}")
    (src / "data" / "resourced.json").write_text("{}")
    (src / "data" / "wrapper.gcode").write_text("")
    (src / "data" / "deep" / "nested.json").write_text("{}")
    (src / "data" / "deep" / "README.md").write_text("")
    return pkg


def _wheel_with(tmp_path: Path, pkg: Path, *, drop: set[str] = frozenset()) -> set[str]:
    """Build a fake wheel from every tracked file except ``drop``; return its names."""
    names = {n for n in gate._source_files(pkg / "src") if n not in drop}
    path = tmp_path / "fake-0.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as zf:
        for n in names:
            zf.writestr(n, "")
    with zipfile.ZipFile(path) as zf:
        return set(zf.namelist())


_EXCLUDE_DOCS = [("kiln/data/deep/*.md", "docs")]


def test_code_reads_are_found_through_every_idiom(tmp_path):
    pkg = _make_tree(tmp_path)
    reads = gate._code_read_files(pkg / "src")
    assert set(reads) == {
        "kiln/pro_tool_manifest.json",      # Path(__file__).parent / "..."
        "kiln/data/catalog.json",           # a module-level _DATA_DIR constant
        "kiln/data/joined.json",            # os.path.join(os.path.dirname(__file__), ...)
        "kiln/beside_helper.txt",           # another package module's __file__
        "kiln/data/resourced.json",         # importlib.resources.files("kiln")
    }
    # Each read names the line that reads it, so a finding says where to look.
    assert reads["kiln/pro_tool_manifest.json"] == ["kiln/server.py:10"]


def test_a_file_the_code_reads_must_ship_and_no_exclusion_can_wave_it_through(tmp_path):
    """The 2026-10-02 shape: read beside the code, in no glob."""
    pkg = _make_tree(tmp_path)
    names = _wheel_with(tmp_path, pkg, drop={"kiln/pro_tool_manifest.json"})
    result = gate._audit_derived(
        names, pkg, exclusions=_EXCLUDE_DOCS + [("kiln/pro_tool_manifest.json", "nope")],
    )
    assert [n for n, _ in result.missing] == ["kiln/pro_tool_manifest.json"]
    assert "read by kiln/server.py:10" in result.missing[0][1]
    # The exclusion naming it was never consulted, so it reads as stale.
    assert ("kiln/pro_tool_manifest.json", "nope") in result.stale_exclusions
    assert not result.ok


def test_a_tracked_file_in_a_subdirectory_must_ship(tmp_path):
    """The 2026-05-19 shape: a non-recursive glob drops a nested file."""
    pkg = _make_tree(tmp_path)
    names = _wheel_with(tmp_path, pkg, drop={"kiln/data/deep/nested.json"})
    result = gate._audit_derived(names, pkg, exclusions=_EXCLUDE_DOCS)
    assert result.missing == [("kiln/data/deep/nested.json", "in the source tree, on no exclusion")]


def test_an_exclusion_with_a_reason_keeps_a_file_out(tmp_path):
    pkg = _make_tree(tmp_path)
    names = _wheel_with(tmp_path, pkg, drop={"kiln/data/deep/README.md"})
    result = gate._audit_derived(names, pkg, exclusions=_EXCLUDE_DOCS)
    assert result.ok
    assert result.excluded == 1


def test_an_exclusion_that_matches_nothing_fails(tmp_path):
    pkg = _make_tree(tmp_path)
    names = _wheel_with(tmp_path, pkg)
    result = gate._audit_derived(
        names, pkg, exclusions=_EXCLUDE_DOCS + [("kiln/data/gone/*", "deleted long ago")],
    )
    assert result.stale_exclusions == [("kiln/data/gone/*", "deleted long ago")]
    assert not result.ok


def test_a_complete_wheel_passes_and_the_scan_was_not_vacuous(tmp_path):
    pkg = _make_tree(tmp_path)
    names = _wheel_with(tmp_path, pkg)
    result = gate._audit_derived(names, pkg, exclusions=_EXCLUDE_DOCS)
    assert result.ok
    assert result.source_files == 11
    assert result.code_reads == 5


# ---------------------------------------------------------------------------
# The real wheel, built from this tree
# ---------------------------------------------------------------------------


def _copy_package_for_build(dest: Path) -> Path:
    """The package inputs alone, so a build here cannot race one in the tree."""
    pkg = dest / "kiln"
    pkg.mkdir()
    for item in _PACKAGE_DIR.iterdir():
        if item.is_file():
            shutil.copy2(item, pkg / item.name)
    shutil.copytree(
        _PACKAGE_DIR / "src", pkg / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", "*.pyc"),
    )
    return pkg


def test_the_built_wheel_carries_every_file_the_code_reads(tmp_path):
    """Builds kiln3d from this checkout and audits the result.

    Fails, not skips, when the wheel cannot be built: a gate that stands
    down quietly is how this bug class survived two releases' worth of
    green CI.  The build takes a few seconds with a warm build backend.
    """
    pkg = _copy_package_for_build(tmp_path)
    wheel = gate._build_wheel(pkg, tmp_path / "dist")
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())

    # Derived from the REAL tree (git lists it); the copy was only built.
    result = gate._audit_derived(names, _PACKAGE_DIR)

    assert not result.missing, "\n" + "\n".join(f"{n}  ({why})" for n, why in result.missing)
    assert not result.stale_exclusions, result.stale_exclusions
    # The scan has to have seen the file this gate was built for.
    assert "kiln/pro_tool_manifest.json" in names
    assert result.code_reads >= 10, result.code_reads
    assert result.source_files >= 500, result.source_files


def test_the_fixed_list_is_still_checked_too():
    """The hand-written groups stay: a floor catches a tracked file DELETED."""
    assert any(g.name == "design_knowledge" for g in gate.EXPECTED_GROUPS)
