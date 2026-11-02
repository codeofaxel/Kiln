"""kiln.edit_lineage on an install without kiln-pro: every edit tool answers
exactly as it always has."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest


@pytest.fixture()
def no_kiln_pro(monkeypatch):
    """An install without kiln-pro: every import of it fails."""
    import builtins

    real_import = builtins.__import__

    def _import(name, *args, **kwargs):
        if name == "kiln_pro" or name.startswith("kiln_pro."):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _import)
    # Nor is it already loaded, as it is when another test imported it.
    monkeypatch.delitem(sys.modules, "kiln_pro", raising=False)


def test_install_does_nothing_without_kiln_pro(no_kiln_pro):
    from kiln import edit_lineage

    registry = SimpleNamespace(_tool_manager=SimpleNamespace(_tools={"rescale_model": object()}))
    assert edit_lineage.install(registry) == {"installed": False}


@pytest.fixture()
def edit_doors(monkeypatch):
    """The edit doors as ``kiln serve`` loads them from the bundled manifest,
    set here rather than left to whichever test imported the server first."""
    import json
    from pathlib import Path

    from kiln import edit_lineage

    for name, value in (("_doors", {}), ("_people", set()), ("_notes", {}), ("_output_keys", ())):
        monkeypatch.setattr(edit_lineage, name, value)
    manifest = Path(edit_lineage.__file__).with_name("pro_tool_manifest.json")
    edit_lineage.load(json.loads(manifest.read_text(encoding="utf-8")).get("edit_lineage"))
    return edit_lineage


def test_around_hands_the_result_back_untouched(no_kiln_pro, edit_doors, tmp_path):
    """The door's own answer comes back as it was made; an edit that left
    the file as it was adds only the line saying nothing new was kept."""
    part = tmp_path / "part.stl"
    part.write_bytes(b"solid x\nendsolid x\n")
    result = {"success": True, "path": str(part)}
    with edit_doors.around("kiln repair", str(part)) as edit:
        assert edit.finish(result) is result
    kept = result.pop(edit_doors.SAVED_VERSION_KEY)
    assert result == {"success": True, "path": str(part)}
    assert kept["recorded"] is False and kept["unchanged"] is True


def test_around_keeps_nothing_when_the_edit_raises(no_kiln_pro, tmp_path):
    from kiln import edit_lineage

    with pytest.raises(ValueError):
        with edit_lineage.around("kiln repair", str(tmp_path / "x.stl")):
            raise ValueError("bad mesh")
