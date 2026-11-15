"""Slicer profiles are written into a fresh folder made for them, never into
a folder with a fixed name in the shared temp directory.

A fixed name can be made first by anything else on the machine (on the
shared server, by another customer's call), and a profile carries the start
sequence the printer runs.  On the shared server the folder sits in the
caller's own scratch folder, and a cached profile is never handed to a call
working under a different folder.  Every folder is removed at exit.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiln import output_paths, slicer_profiles


@pytest.fixture()
def fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(slicer_profiles, "_temp_cache", {})
    monkeypatch.setattr(slicer_profiles, "_profile_dirs", [], raising=False)
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    return tmp_path


def _three_profiles() -> list[str]:
    return [
        slicer_profiles.resolve_slicer_profile("bambu_a1"),
        slicer_profiles.profile_with_overrides(None, {"layer_height": "0.16"}),
        slicer_profiles.resolve_multiextruder_profile("bambu_a1", 2),
    ]


def test_each_profile_sits_in_a_random_folder_of_its_own(fresh):
    paths = _three_profiles()
    folders = [Path(p).parent for p in paths]
    for folder in folders:
        assert folder.parent == fresh
        assert folder.name.startswith("kiln_slicer_") and folder.name != "kiln_slicer_profiles"
        assert oct(folder.stat().st_mode & 0o777) == "0o700"
    assert len(set(folders)) == 3
    assert not (fresh / "kiln_slicer_profiles").exists()


def test_a_folder_made_first_by_someone_else_is_never_written_into(fresh):
    planted = fresh / "kiln_slicer_profiles"
    planted.mkdir()
    _three_profiles()
    assert list(planted.iterdir()) == []


def test_a_call_under_another_folder_never_gets_a_cached_path(fresh):
    first_root = fresh / "caller_a"
    second_root = fresh / "caller_b"
    first_root.mkdir()
    second_root.mkdir()
    token = output_paths.REQUEST_OUTPUT_ROOT.set(str(first_root))
    try:
        a = slicer_profiles.resolve_slicer_profile("bambu_a1")
        assert slicer_profiles.resolve_slicer_profile("bambu_a1") == a  # reused within one folder
    finally:
        output_paths.REQUEST_OUTPUT_ROOT.reset(token)
    token = output_paths.REQUEST_OUTPUT_ROOT.set(str(second_root))
    try:
        b = slicer_profiles.resolve_slicer_profile("bambu_a1")
    finally:
        output_paths.REQUEST_OUTPUT_ROOT.reset(token)
    assert Path(a).is_relative_to(first_root) and Path(b).is_relative_to(second_root)


def test_every_folder_is_removed_at_exit(fresh):
    paths = _three_profiles()
    slicer_profiles._remove_profile_dirs()
    assert not any(os.path.exists(p) or os.path.exists(os.path.dirname(p)) for p in paths)
    assert slicer_profiles._temp_cache == {}
