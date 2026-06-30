"""``kiln doctor``/``verify`` reports OpenSCAD honestly.

Regression guard for the fix: doctor used to label OpenSCAD "optional — needed
for Gemini Deep Think" and mark it ``ok: True`` even when missing, with no
version check.  OpenSCAD is REQUIRED for local OpenSCAD-native design and must be
the 2024+ development snapshot, so doctor must say so and flag an outdated build.
"""
import json

from click.testing import CliRunner

from kiln.cli.main import cli


def _openscad_check(output: str) -> dict:
    data = json.loads(output)
    return next(c for c in data["checks"] if c["name"] == "openscad")


def test_missing_openscad_is_honest_not_optional(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    # platform=linux skips the macOS .app fallback so "missing" is deterministic
    monkeypatch.setattr("kiln.cli.main.sys.platform", "linux")
    monkeypatch.setattr("kiln.cli.main.shutil.which", lambda *a, **k: None)
    res = CliRunner().invoke(cli, ["verify", "--json"])
    chk = _openscad_check(res.output)
    detail = chk["detail"].lower()
    assert "required" in detail
    assert "optional" not in detail and "gemini" not in detail  # the old bug
    assert "install-openscad" in detail  # points at the new helper


def test_outdated_openscad_is_flagged(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("kiln.cli.main.sys.platform", "linux")
    monkeypatch.setattr(
        "kiln.cli.main.shutil.which",
        lambda name=None, *a, **k: "/usr/bin/openscad" if name == "openscad" else None,
    )
    monkeypatch.setattr(
        "kiln.emboss_generator._detect_openscad_version", lambda *a, **k: "2021.01"
    )
    res = CliRunner().invoke(cli, ["verify", "--json"])
    chk = _openscad_check(res.output)
    assert chk.get("warn") is True
    assert "outdated" in chk["detail"].lower()
