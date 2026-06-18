"""The self-update action: safe defer, honest reporting, graceful fallback."""

from __future__ import annotations

from dataclasses import dataclass

from kiln import self_update


@dataclass
class _Proc:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _runner(returncode=0, stdout="", stderr=""):
    captured = {}

    def run(cmd, **kw):
        captured["cmd"] = cmd
        captured["kw"] = kw
        return _Proc(returncode, stdout, stderr)

    run.captured = captured  # type: ignore[attr-defined]
    return run


def test_defers_while_a_print_is_active():
    out = self_update.perform_upgrade(
        runner=_runner(0, "Successfully installed kiln3d-9.9.9"),
        print_active=lambda: True,
    )
    assert out["ok"] is False
    assert out["status"] == "deferred_active_print"
    assert out["restart_required"] is False
    assert "finishes" in out["message"].lower()


def test_force_overrides_the_active_print_defer():
    out = self_update.perform_upgrade(
        runner=_runner(0, "Successfully installed kiln3d-1.4.0"),
        print_active=lambda: True,
        force=True,
    )
    assert out["status"] == "updated"
    assert out["installed"] == "1.4.0"


def test_successful_upgrade_reports_versions_and_restart():
    run = _runner(0, "Collecting kiln3d\nSuccessfully installed kiln3d-1.4.0")
    out = self_update.perform_upgrade(runner=run)
    assert out["ok"] is True
    assert out["status"] == "updated"
    assert out["installed"] == "1.4.0"
    assert out["restart_required"] is True
    assert "restart" in out["message"].lower()
    # targets THIS interpreter's pip, not a bare "pip".
    assert run.captured["cmd"][1:] == ["-m", "pip", "install", "--upgrade", "kiln3d"]


def test_clean_run_with_no_install_line_means_already_latest():
    out = self_update.perform_upgrade(
        runner=_runner(0, "Requirement already satisfied: kiln3d")
    )
    assert out["status"] == "already_latest"
    assert out["restart_required"] is False


def test_pip_failure_hands_back_the_one_liner():
    out = self_update.perform_upgrade(
        runner=_runner(1, "", "ERROR: externally-managed-environment")
    )
    assert out["ok"] is False
    assert out["status"] == "failed"
    assert out["command"] == self_update.UPGRADE_COMMAND
    assert "pip install --upgrade kiln3d" in out["message"]


def test_runner_exception_is_reported_not_raised():
    def boom(cmd, **kw):
        raise OSError("no pip here")

    out = self_update.perform_upgrade(runner=boom)
    assert out["ok"] is False
    assert out["status"] == "failed"
    assert "pip install --upgrade kiln3d" in out["message"]


def test_parse_installed_version_takes_the_last_match():
    assert self_update._parse_installed_version(
        "Successfully installed dep-1.0.0 kiln3d-2.3.4"
    ) == "2.3.4"
    assert self_update._parse_installed_version("nothing here") is None
