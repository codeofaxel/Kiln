"""Learning a slicer's version never starts the slicer's app.

Every Kiln server start reads the slicer's version (the server's opening
instructions name the slicer it found), and the version was read by running
the slicer with ``--version``.  On a Mac, running an app bundle's executable
starts the app as far as macOS is concerned, so with a handful of AI sessions
open the slicer apps bounce in the Dock again and again.  An app bundle's
version is read from its ``Info.plist``.

A/B: the app-bundle tests fail on public ac3ada47 (the stand-in app is
started and writes its mark); the bare-binary test passes on both, the guard
that a slicer outside an app bundle is still asked as before.
"""

from __future__ import annotations

import os
import plistlib
import stat
from pathlib import Path

import pytest

from kiln import slicer

pytestmark = pytest.mark.skipif(os.name == "nt", reason="stand-in slicers are shell scripts")


def _stand_in(path: Path, banner: str, mark: Path) -> str:
    """An executable that records it was started and prints *banner*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\necho started >> "{mark}"\necho "{banner}"\n')
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


def _app(tmp_path: Path, bundle: str, executable: str, plist: dict, mark: Path) -> str:
    contents = tmp_path / f"{bundle}.app" / "Contents"
    binary = _stand_in(contents / "MacOS" / executable, f"{executable}-9.9.9", mark)
    with open(contents / "Info.plist", "wb") as fh:
        plistlib.dump({"CFBundleExecutable": executable, **plist}, fh)
    return binary


@pytest.mark.parametrize(
    ("bundle", "executable", "plist", "version", "family"),
    [
        # The shapes of three real installed apps' plists.
        ("OrcaSlicer", "OrcaSlicer", {"CFBundleName": "OrcaSlicer", "CFBundleShortVersionString": "2.3.2"},
         "OrcaSlicer-2.3.2", "bambu"),
        ("BambuStudio", "BambuStudio",
         {"CFBundleName": "Bambu Studio", "CFBundleShortVersionString": "02.08.02.61"},
         "BambuStudio-02.08.02.61", "bambu"),
        ("PrusaSlicer", "PrusaSlicer",
         {"CFBundleName": "PrusaSlicer", "CFBundleShortVersionString": "PrusaSlicer PrusaSlicer-2.9.4",
          "CFBundleVersion": "PrusaSlicer-2.9.4"},
         "PrusaSlicer-2.9.4", "prusa"),
    ],
)
def test_an_app_bundles_version_is_read_without_starting_it(tmp_path, bundle, executable, plist, version, family):
    mark = tmp_path / "started"
    binary = _app(tmp_path, bundle, executable, plist, mark)
    info = slicer.find_slicer(binary)
    assert not mark.exists(), "the app was started to read its version"
    assert info.version == version
    assert slicer.slicer_cli_family(info) == (slicer._CLI_BAMBU if family == "bambu" else slicer._CLI_PRUSA)


def test_a_link_to_an_app_bundle_is_read_from_the_bundle(tmp_path):
    mark = tmp_path / "started"
    binary = _app(tmp_path, "OrcaSlicer", "OrcaSlicer", {"CFBundleShortVersionString": "2.3.2"}, mark)
    link = tmp_path / "bin" / "orca-slicer"
    link.parent.mkdir()
    link.symlink_to(binary)
    assert slicer.find_slicer(str(link)).version == "OrcaSlicer-2.3.2"
    assert not mark.exists()


def test_a_bundle_whose_plist_names_no_version_is_asked_the_old_way(tmp_path):
    mark = tmp_path / "started"
    binary = _app(tmp_path, "Odd", "OddSlicer", {"CFBundleName": "Odd"}, mark)
    assert slicer.find_slicer(binary).version == "OddSlicer-9.9.9"
    assert mark.exists()


def test_a_bare_binary_is_still_asked_for_its_version(tmp_path):
    """A guard, true on main too: a slicer outside an app bundle (Linux, a
    build on the PATH) has no plist, and its own banner is still read."""
    mark = tmp_path / "started"
    binary = _stand_in(tmp_path / "bin" / "prusa-slicer", "PrusaSlicer-2.9.4 based on Slic3r", mark)
    info = slicer.find_slicer(binary)
    assert info.version == "PrusaSlicer-2.9.4 based on Slic3r"
    assert mark.exists()
