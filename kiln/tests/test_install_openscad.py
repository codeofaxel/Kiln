"""Tests for ``kiln install-openscad`` — the OpenSCAD-snapshot helper."""
from unittest import mock

import click
from click.testing import CliRunner

from kiln.cli.install_openscad import (
    install_openscad,
    register_install_openscad_cli,
)

_MOD = "kiln.cli.install_openscad"


def test_registers_command():
    group = click.Group()
    register_install_openscad_cli(group)
    assert "install-openscad" in group.commands


def test_already_current_skips_install():
    """A current snapshot already present -> no install attempted."""
    with mock.patch(f"{_MOD}._current_openscad", return_value=("/usr/bin/openscad", "2024.12.19", 2024)), \
         mock.patch(f"{_MOD}._min_year", return_value=2024), \
         mock.patch(f"{_MOD}._run") as run:
        res = CliRunner().invoke(install_openscad, [])
    assert res.exit_code == 0
    assert "already current" in res.output
    run.assert_not_called()


def test_force_reinstalls_even_when_current(monkeypatch):
    monkeypatch.setattr(f"{_MOD}.sys.platform", "darwin")
    with mock.patch(f"{_MOD}._current_openscad", return_value=("/x/openscad", "2024.1.1", 2024)), \
         mock.patch(f"{_MOD}._min_year", return_value=2024), \
         mock.patch(f"{_MOD}.shutil.which", return_value="/opt/homebrew/bin/brew"), \
         mock.patch(f"{_MOD}._run", return_value=(True, "")) as run:
        res = CliRunner().invoke(install_openscad, ["--force"])
    assert res.exit_code == 0
    run.assert_called_once()  # --force runs the install despite being current


def test_macos_without_brew_shows_manual(monkeypatch):
    monkeypatch.setattr(f"{_MOD}.sys.platform", "darwin")
    with mock.patch(f"{_MOD}._current_openscad", return_value=(None, "", 0)), \
         mock.patch(f"{_MOD}._min_year", return_value=2024), \
         mock.patch(f"{_MOD}.shutil.which", return_value=None), \
         mock.patch(f"{_MOD}._run") as run:
        res = CliRunner().invoke(install_openscad, [])
    assert res.exit_code == 0
    assert "openscad.org/downloads#snapshots" in res.output
    run.assert_not_called()  # no brew -> no blind auto-install


def test_macos_with_brew_installs_snapshot_and_verifies(monkeypatch):
    monkeypatch.setattr(f"{_MOD}.sys.platform", "darwin")
    # missing before the install, current after it
    states = [(None, "", 0), ("/opt/homebrew/bin/openscad", "2024.12.19", 2024)]
    with mock.patch(f"{_MOD}._current_openscad", side_effect=states), \
         mock.patch(f"{_MOD}._min_year", return_value=2024), \
         mock.patch(f"{_MOD}.shutil.which", return_value="/opt/homebrew/bin/brew"), \
         mock.patch(f"{_MOD}._run", return_value=(True, "")) as run:
        res = CliRunner().invoke(install_openscad, [])
    assert res.exit_code == 0
    run.assert_called_once()
    assert "openscad@snapshot" in " ".join(run.call_args[0][0])
    assert "installed and current" in res.output


def test_linux_without_snap_shows_manual(monkeypatch):
    monkeypatch.setattr(f"{_MOD}.sys.platform", "linux")
    with mock.patch(f"{_MOD}._current_openscad", return_value=(None, "", 0)), \
         mock.patch(f"{_MOD}._min_year", return_value=2024), \
         mock.patch(f"{_MOD}.shutil.which", return_value=None), \
         mock.patch(f"{_MOD}._run") as run:
        res = CliRunner().invoke(install_openscad, [])
    assert res.exit_code == 0
    assert "openscad.org/downloads#snapshots" in res.output
    run.assert_not_called()


def test_install_failure_is_reported_honestly(monkeypatch):
    """Install command runs but the binary still isn't current -> honest fail."""
    monkeypatch.setattr(f"{_MOD}.sys.platform", "darwin")
    with mock.patch(f"{_MOD}._current_openscad", return_value=(None, "", 0)), \
         mock.patch(f"{_MOD}._min_year", return_value=2024), \
         mock.patch(f"{_MOD}.shutil.which", return_value="/opt/homebrew/bin/brew"), \
         mock.patch(f"{_MOD}._run", return_value=(False, "brew: error: cask not found")):
        res = CliRunner().invoke(install_openscad, [])
    assert res.exit_code == 0
    assert "didn't complete" in res.output
    assert "openscad.org/downloads#snapshots" in res.output  # manual fallback shown
