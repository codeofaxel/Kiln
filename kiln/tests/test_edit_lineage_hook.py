"""kiln.edit_lineage on an install without kiln-pro: every edit tool answers
exactly as it always has."""

from __future__ import annotations

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


def test_install_does_nothing_without_kiln_pro(no_kiln_pro):
    from kiln import edit_lineage

    registry = SimpleNamespace(_tool_manager=SimpleNamespace(_tools={"rescale_model": object()}))
    assert edit_lineage.install(registry) == {"installed": False}


def test_around_hands_the_result_back_untouched(no_kiln_pro, tmp_path):
    from kiln import edit_lineage

    part = tmp_path / "part.stl"
    part.write_bytes(b"solid x\nendsolid x\n")
    result = {"success": True, "path": str(part)}
    with edit_lineage.around("kiln repair", str(part)) as edit:
        assert edit.finish(result) is result
    assert result == {"success": True, "path": str(part)}


def test_around_keeps_nothing_when_the_edit_raises(no_kiln_pro, tmp_path):
    from kiln import edit_lineage

    with pytest.raises(ValueError):
        with edit_lineage.around("kiln repair", str(tmp_path / "x.stl")):
            raise ValueError("bad mesh")
